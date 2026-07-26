"""Captura técnica de páginas web em PDF, com interface local e publicação opcional no GitHub.

Execute ``python PDF_printer.py`` para abrir a interface em http://127.0.0.1:8765.
Execute ``python PDF_printer.py --help`` para o modo automatizado/linha de comando.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template_string, request, send_from_directory, url_for
from PIL import Image, ImageOps
from playwright.sync_api import Page, sync_playwright
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


APP_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = APP_DIR / "capturas"
DEFAULT_BRANCH = "main"
MAX_GITHUB_FILE_BYTES = 95 * 1024 * 1024
CAPTURE_LOCK = threading.Lock()
GITHUB_AUTH_LOCK = threading.Lock()
GITHUB_AUTH_STATE: dict[str, Any] = {"running": False, "message": "", "device_code": ""}


class CaptureError(RuntimeError):
    """Erro que pode ser exibido de forma clara na interface."""


class GitHubPublishError(CaptureError):
    """Erro ao enviar a captura para o GitHub, sem invalidar a captura local."""


@dataclass
class CaptureResult:
    capture_id: str
    output_dir: Path
    pdf_path: Path
    metadata_path: Path
    integrity_path: Path
    title: str
    final_url: str
    captured_at_utc: str
    youtube_position_seconds: float | None
    github_commit_url: str | None = None
    github_warning: str | None = None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def slugify(value: str, fallback: str = "captura") -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip())
    value = re.sub(r"-{2,}", "-", value).strip("-_").lower()
    return value[:70] or fallback


def validate_url(raw_url: str) -> str:
    url = raw_url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CaptureError("Informe uma URL completa que comece com http:// ou https://.")
    return url


def parse_time_value(value: str) -> int | None:
    """Converte 2771, 46m11s, 1h02m03s e formatos equivalentes para segundos."""
    value = urllib.parse.unquote_plus(value).strip().lower()
    if not value:
        return None
    if value.isdecimal():
        return int(value)

    match = re.fullmatch(
        r"(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?", value
    )
    if not match or not any(match.groupdict().values()):
        return None
    return (
        int(match.group("h") or 0) * 3600
        + int(match.group("m") or 0) * 60
        + int(match.group("s") or 0)
    )


def youtube_start_seconds(url: str) -> int | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    is_youtube = host == "youtu.be" or host.endswith(".youtube.com") or host == "youtube.com"
    if not is_youtube:
        return None

    query = urllib.parse.parse_qs(parsed.query)
    for key in ("t", "start"):
        if query.get(key):
            seconds = parse_time_value(query[key][0])
            if seconds is not None:
                return seconds

    # Links compartilhados também podem trazer o tempo após #t=.
    fragment = urllib.parse.parse_qs(parsed.fragment)
    if fragment.get("t"):
        return parse_time_value(fragment["t"][0])
    return None


def human_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "não informado"
    total = max(0, round(float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}min{seconds:02d}s"
    return f"{minutes}min{seconds:02d}s"


def _click_if_present(page: Page, selector: str) -> None:
    try:
        button = page.locator(selector).first
        if button.count() > 0 and button.is_visible():
            button.click(timeout=2_500)
            page.wait_for_timeout(800)
    except Exception:
        pass


def dismiss_common_consent(page: Page) -> None:
    # São tentativas inofensivas: a página continua a ser capturada mesmo que não haja botão.
    for selector in (
        "button:has-text('Aceitar tudo')",
        "button:has-text('Aceitar todos')",
        "button:has-text('I agree')",
        "button:has-text('Accept all')",
        "button:has-text('Pular anúncios')",
        "button:has-text('Skip ads')",
    ):
        _click_if_present(page, selector)


def seek_youtube_video(page: Page, requested_seconds: int) -> float | None:
    """Pausa o vídeo no ponto pedido e devolve a posição efetivamente obtida."""
    try:
        page.locator("video").first.wait_for(state="attached", timeout=20_000)
        result = page.evaluate(
            """async (requested) => {
                const video = document.querySelector('video');
                if (!video) return { ok: false };

                const duration = Number.isFinite(video.duration) ? video.duration : null;
                const target = duration === null
                    ? requested
                    : Math.min(Math.max(0, requested), Math.max(0, duration - 0.05));

                await new Promise((resolve) => {
                    let done = false;
                    const finish = () => {
                        if (done) return;
                        done = true;
                        video.removeEventListener('seeked', finish);
                        resolve();
                    };
                    video.addEventListener('seeked', finish, { once: true });
                    window.setTimeout(finish, 8000);
                    try {
                        video.currentTime = target;
                    } catch (_) {
                        finish();
                    }
                });
                video.pause();
                return { ok: true, currentTime: video.currentTime, duration };
            }""",
            requested_seconds,
        )
        page.wait_for_timeout(1_200)
        if result and result.get("ok"):
            return float(result.get("currentTime", 0))
    except Exception:
        return None
    return None


def scroll_page_for_lazy_content(page: Page) -> None:
    """Percorre a página uma vez para carregar conteúdo que só aparece durante a rolagem."""
    try:
        page.evaluate(
            """async () => {
                const delay = (ms) => new Promise(resolve => setTimeout(resolve, ms));
                const step = Math.max(500, Math.floor(window.innerHeight * 0.8));
                const maximum = Math.max(
                    document.body.scrollHeight,
                    document.documentElement.scrollHeight
                );
                for (let y = 0; y < maximum; y += step) {
                    window.scrollTo(0, y);
                    await delay(110);
                }
                window.scrollTo(0, 0);
                await delay(350);
            }"""
        )
    except Exception:
        # Uma página que bloqueia script ainda pode ser capturada pelo Playwright.
        pass


def screenshot_video_or_viewport(page: Page, target: Path) -> str:
    for selector in ("#movie_player", ".html5-video-player", "video"):
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                locator.screenshot(path=str(target))
                return selector
        except Exception:
            pass
    page.screenshot(path=str(target), full_page=False)
    return "viewport"


def draw_image_slices(pdf: canvas.Canvas, image_path: Path, title: str) -> None:
    """Desenha uma captura longa em várias páginas A4, sem reduzir o conteúdo a uma miniatura."""
    page_width, page_height = A4
    margin = 1.25 * cm
    title_y = page_height - margin
    max_width = page_width - (2 * margin)
    max_height = page_height - (3.0 * margin)

    with Image.open(image_path) as original:
        image = ImageOps.exif_transpose(original)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")
        width, height = image.size
        scale = max_width / width
        source_slice_height = max(1, int(max_height / scale))
        page_count = max(1, math.ceil(height / source_slice_height))

        for index in range(page_count):
            upper = index * source_slice_height
            lower = min(height, upper + source_slice_height)
            crop = image.crop((0, upper, width, lower))
            buffer = BytesIO()
            crop.save(buffer, format="PNG")
            buffer.seek(0)

            draw_height = crop.height * scale
            pdf.setFont("Helvetica-Bold", 11)
            pdf.drawString(margin, title_y, title)
            pdf.setFont("Helvetica", 8)
            pdf.drawRightString(
                page_width - margin,
                title_y,
                f"trecho {index + 1} de {page_count}",
            )
            pdf.drawImage(
                ImageReader(buffer),
                margin,
                title_y - (0.55 * cm) - draw_height,
                width=max_width,
                height=draw_height,
                preserveAspectRatio=True,
                mask="auto",
            )
            pdf.showPage()


def draw_single_image(pdf: canvas.Canvas, image_path: Path, title: str) -> None:
    page_width, page_height = A4
    margin = 1.25 * cm
    max_width = page_width - (2 * margin)
    max_height = page_height - (3.2 * margin)

    source = ImageReader(str(image_path))
    image_width, image_height = source.getSize()
    scale = min(max_width / image_width, max_height / image_height)
    draw_width, draw_height = image_width * scale, image_height * scale

    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin, page_height - margin, title)
    pdf.drawImage(
        source,
        (page_width - draw_width) / 2,
        (page_height - draw_height) / 2 - (0.25 * cm),
        width=draw_width,
        height=draw_height,
        preserveAspectRatio=True,
        mask="auto",
    )
    pdf.showPage()


def write_report(pdf: canvas.Canvas, metadata: dict[str, Any]) -> None:
    page_width, page_height = A4
    margin = 1.45 * cm
    y = page_height - margin

    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(margin, y, "RELATÓRIO TÉCNICO DE CAPTURA")
    y -= 0.9 * cm

    report_rows = (
        ("URL informada", metadata["url_informada"]),
        ("URL final acessada", metadata["url_final_acessada"]),
        ("Título da página", metadata["titulo_pagina"] or "não identificado"),
        ("Status HTTP", str(metadata["status_http"] or "não disponível")),
        ("Data e hora da captura (UTC)", metadata["data_hora_captura_utc"]),
        ("YouTube - tempo indicado", metadata["youtube_tempo_indicado"]),
        ("YouTube - posição capturada", metadata["youtube_posicao_capturada"]),
        ("SHA-256 do HTML", metadata["sha256_html"]),
        ("SHA-256 da imagem integral", metadata["sha256_imagem_integral"]),
        ("SHA-256 da imagem do player", metadata["sha256_imagem_player"]),
        ("Ferramenta", metadata["ferramenta"]),
    )

    def new_page() -> float:
        pdf.showPage()
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(margin, page_height - margin, "RELATÓRIO TÉCNICO DE CAPTURA (continuação)")
        return page_height - (2.1 * cm)

    for label, value in report_rows:
        lines = [f"{label}:"]
        text = str(value)
        lines.extend("  " + line for line in _wrap_text(text, 98))
        lines.append("")
        for line in lines:
            if y < 1.7 * cm:
                y = new_page()
            pdf.setFont("Helvetica-Bold" if line == f"{label}:" else "Helvetica", 8.8)
            pdf.drawString(margin, y, line)
            y -= 0.43 * cm

    observations = (
        "Observação técnica: o PDF agrega a visualização integral da página, uma imagem do "
        "player quando aplicável, o HTML e os metadados como anexos. O arquivo "
        "integridade.sha256, entregue ao lado do PDF, permite verificar a versão final. "
        "Esta captura não substitui ata notarial, assinatura digital qualificada ou carimbo "
        "oficial do tempo."
    )
    for line in _wrap_text(observations, 98):
        if y < 1.7 * cm:
            y = new_page()
        pdf.setFont("Helvetica", 8.8)
        pdf.drawString(margin, y, line)
        y -= 0.43 * cm


def _wrap_text(value: str, width: int) -> list[str]:
    words = value.replace("\r", "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        if len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.extend(word[i : i + width] for i in range(0, len(word), width))
        elif len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def create_base_pdf(
    target: Path,
    full_page_png: Path,
    player_png: Path,
    metadata: dict[str, Any],
) -> None:
    pdf = canvas.Canvas(str(target), pagesize=A4)
    draw_image_slices(pdf, full_page_png, "CAPTURA VISUAL INTEGRAL DA PÁGINA")
    draw_single_image(pdf, player_png, "CAPTURA DO PLAYER / VÍDEO")
    write_report(pdf, metadata)
    pdf.save()


def create_final_pdf(
    base_pdf: Path,
    final_pdf: Path,
    attachments: list[Path],
    metadata: dict[str, Any],
) -> None:
    reader = PdfReader(str(base_pdf))
    writer = PdfWriter()
    for item in reader.pages:
        writer.add_page(item)

    writer.add_metadata(
        {
            "/Title": "Captura técnica consolidada de página web",
            "/Subject": f"Captura técnica da URL: {metadata['url_informada']}",
            "/Author": "PDF-printer",
            "/Creator": "Python + Playwright + Chromium + ReportLab + pypdf",
            "/Producer": "pypdf",
            "/Keywords": (
                f"URL_INFORMADA={metadata['url_informada']}; "
                f"URL_FINAL={metadata['url_final_acessada']}; "
                f"DATA_UTC={metadata['data_hora_captura_utc']}; "
                f"SHA256_HTML={metadata['sha256_html']}; "
                f"SHA256_PDF_BASE={metadata['sha256_pdf_base_antes_dos_anexos']}"
            ),
        }
    )
    for attachment in attachments:
        writer.add_attachment(attachment.name, attachment.read_bytes())

    with final_pdf.open("wb") as destination:
        writer.write(destination)


def capture_url(url: str, output_root: Path = DEFAULT_OUTPUT_DIR, label: str = "") -> CaptureResult:
    url = validate_url(url)
    now = datetime.now(timezone.utc)
    captured_at_utc = now.isoformat(timespec="seconds")
    host = urllib.parse.urlparse(url).hostname or "pagina"
    capture_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}_{slugify(label or host)}"
    output_dir = output_root.resolve() / capture_id
    output_dir.mkdir(parents=True, exist_ok=False)

    full_page_png = output_dir / "pagina_integral.png"
    player_png = output_dir / "player_ou_viewport.png"
    html_path = output_dir / "pagina_original.html"
    metadata_path = output_dir / "metadados_captura.json"
    base_pdf = output_dir / "_base_sem_anexos.pdf"
    final_pdf = output_dir / "captura_consolidada.pdf"
    integrity_path = output_dir / "integridade.sha256"

    final_url = url
    title = ""
    status_http: int | None = None
    html_content = ""
    player_selector = "viewport"
    requested_youtube_time = youtube_start_seconds(url)
    captured_youtube_time: float | None = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    device_scale_factor=1,
                    locale="pt-BR",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(3_000)
                final_url = page.url
                title = page.title()
                status_http = response.status if response else None
                dismiss_common_consent(page)

                if requested_youtube_time is not None:
                    captured_youtube_time = seek_youtube_video(page, requested_youtube_time)

                player_selector = screenshot_video_or_viewport(page, player_png)
                scroll_page_for_lazy_content(page)
                page.screenshot(path=str(full_page_png), full_page=True, animations="disabled")
                html_content = page.content()
                context.close()
            finally:
                browser.close()
    except Exception as error:
        raise CaptureError(f"Não foi possível abrir e capturar a página: {error}") from error

    html_path.write_text(html_content, encoding="utf-8", errors="replace")
    metadata: dict[str, Any] = {
        "versao_formato": "1.0",
        "url_informada": url,
        "url_final_acessada": final_url,
        "titulo_pagina": title,
        "status_http": status_http,
        "data_hora_captura_utc": captured_at_utc,
        "youtube_tempo_indicado_segundos": requested_youtube_time,
        "youtube_tempo_indicado": human_duration(requested_youtube_time),
        "youtube_posicao_capturada_segundos": captured_youtube_time,
        "youtube_posicao_capturada": human_duration(captured_youtube_time),
        "seletor_usado_na_captura_do_player": player_selector,
        "ferramenta": "Python + Playwright + Chromium + ReportLab + pypdf",
        "sha256_html": sha256_file(html_path),
        "sha256_imagem_integral": sha256_file(full_page_png),
        "sha256_imagem_player": sha256_file(player_png),
    }

    create_base_pdf(base_pdf, full_page_png, player_png, metadata)
    metadata["sha256_pdf_base_antes_dos_anexos"] = sha256_file(base_pdf)
    metadata_path.write_bytes(json_bytes(metadata))
    create_final_pdf(
        base_pdf,
        final_pdf,
        [html_path, metadata_path, full_page_png, player_png],
        metadata,
    )
    base_pdf.unlink(missing_ok=True)

    final_hash = sha256_file(final_pdf)
    integrity_path.write_text(
        "# Verificação de integridade — SHA-256\n"
        f"{final_hash}  {final_pdf.name}\n"
        f"{sha256_file(metadata_path)}  {metadata_path.name}\n"
        f"{sha256_file(full_page_png)}  {full_page_png.name}\n"
        f"{sha256_file(player_png)}  {player_png.name}\n"
        f"{sha256_file(html_path)}  {html_path.name}\n",
        encoding="utf-8",
    )

    return CaptureResult(
        capture_id=capture_id,
        output_dir=output_dir,
        pdf_path=final_pdf,
        metadata_path=metadata_path,
        integrity_path=integrity_path,
        title=title,
        final_url=final_url,
        captured_at_utc=captured_at_utc,
        youtube_position_seconds=captured_youtube_time,
    )


def github_api_request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_object = urllib.request.Request(
        f"https://api.github.com{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PDF-printer",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request_object, timeout=50) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            details = json.loads(error.read().decode("utf-8")).get("message", "")
        except Exception:
            details = ""
        raise GitHubPublishError(
            f"GitHub respondeu HTTP {error.code}{': ' + details if details else ''}."
        ) from error
    except urllib.error.URLError as error:
        raise GitHubPublishError(f"Não foi possível conectar ao GitHub: {error.reason}") from error


def github_cli_token() -> str:
    """Lê o token da sessão já autenticada no GitHub CLI, sem gravá-lo ou exibi-lo."""
    try:
        completed = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def github_cli_account() -> str:
    """Retorna somente o login da conta ativa no GitHub CLI."""
    try:
        completed = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
        login = completed.stdout.strip()
        return login if re.fullmatch(r"[A-Za-z0-9-]+", login) else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def start_github_login() -> dict[str, str | bool]:
    """Inicia a autorização web do GitHub CLI sem revelar tokens no aplicativo."""
    with GITHUB_AUTH_LOCK:
        if GITHUB_AUTH_STATE["running"]:
            return dict(GITHUB_AUTH_STATE)
        GITHUB_AUTH_STATE.update(
            running=True,
            message="Abrindo a autorização do GitHub no navegador…",
            device_code="",
        )

    def login_worker() -> None:
        output: list[str] = []
        try:
            options: dict[str, Any] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(
                [
                    "gh",
                    "auth",
                    "login",
                    "--hostname",
                    "github.com",
                    "--web",
                    "--git-protocol",
                    "https",
                    "--skip-ssh-key",
                ],
                **options,
            )
            assert process.stdout is not None
            for line in process.stdout:
                output.append(line)
                match = re.search(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", line)
                if match:
                    with GITHUB_AUTH_LOCK:
                        GITHUB_AUTH_STATE["device_code"] = match.group(0)
                        GITHUB_AUTH_STATE["message"] = "Informe este código na página aberta do GitHub."
            exit_code = process.wait()
            account = github_cli_account()
            message = (
                f"Conta @{account} conectada." if exit_code == 0 and account
                else "A conexão não foi concluída. Tente novamente."
            )
        except OSError:
            message = "Não foi possível iniciar o GitHub CLI nesta máquina."
        with GITHUB_AUTH_LOCK:
            GITHUB_AUTH_STATE.update(running=False, message=message, device_code="")

    threading.Thread(target=login_worker, daemon=True).start()
    return dict(GITHUB_AUTH_STATE)


def github_auth_status() -> dict[str, str | bool]:
    with GITHUB_AUTH_LOCK:
        state = dict(GITHUB_AUTH_STATE)
    state["account"] = github_cli_account()
    return state


def connected_github_repository() -> str:
    """Sugere o repositório remoto desta cópia do aplicativo, se houver um."""
    configured = os.environ.get("PDF_PRINTER_GITHUB_REPOSITORY", "").strip()
    if configured:
        return configured
    try:
        completed = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
        remote = completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", remote)
    return match.group(1) if match else ""


def publish_capture_to_github(
    result: CaptureResult,
    repository: str,
    branch: str = DEFAULT_BRANCH,
    token: str | None = None,
) -> str:
    """Cria um único commit contendo todos os artefatos da captura em repositório público."""
    repository = repository.strip()
    branch = branch.strip() or DEFAULT_BRANCH
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise GitHubPublishError("Informe o repositório no formato proprietario/repositorio.")
    token = (token or os.environ.get("GITHUB_TOKEN") or github_cli_token()).strip()
    if not token:
        raise GitHubPublishError(
            "Informe um token do GitHub com permissão Contents: Read and write, "
            "defina GITHUB_TOKEN ou conecte-se com 'gh auth login'."
        )

    repo_info = github_api_request("GET", f"/repos/{repository}", token)
    if repo_info.get("private"):
        raise GitHubPublishError("O repositório indicado é privado; escolha um repositório público.")

    ref_path = urllib.parse.quote(branch, safe="")
    ref = github_api_request("GET", f"/repos/{repository}/git/ref/heads/{ref_path}", token)
    parent_commit = ref["object"]["sha"]
    commit_info = github_api_request("GET", f"/repos/{repository}/git/commits/{parent_commit}", token)
    base_tree = commit_info["tree"]["sha"]

    files = sorted(path for path in result.output_dir.iterdir() if path.is_file())
    oversized = [path.name for path in files if path.stat().st_size > MAX_GITHUB_FILE_BYTES]
    if oversized:
        raise GitHubPublishError(
            "O GitHub não aceita um ou mais arquivos acima de 95 MiB: " + ", ".join(oversized)
        )

    tree_entries: list[dict[str, str]] = []
    for file_path in files:
        blob = github_api_request(
            "POST",
            f"/repos/{repository}/git/blobs",
            token,
            {
                "content": base64.b64encode(file_path.read_bytes()).decode("ascii"),
                "encoding": "base64",
            },
        )
        tree_entries.append(
            {
                "path": f"capturas/{result.capture_id}/{file_path.name}",
                "mode": "100644",
                "type": "blob",
                "sha": blob["sha"],
            }
        )

    tree = github_api_request(
        "POST",
        f"/repos/{repository}/git/trees",
        token,
        {"base_tree": base_tree, "tree": tree_entries},
    )
    label = result.title or result.capture_id
    commit = github_api_request(
        "POST",
        f"/repos/{repository}/git/commits",
        token,
        {
            "message": f"Preserva captura: {label[:100]}",
            "tree": tree["sha"],
            "parents": [parent_commit],
        },
    )
    github_api_request(
        "PATCH",
        f"/repos/{repository}/git/refs/heads/{ref_path}",
        token,
        {"sha": commit["sha"], "force": False},
    )
    return f"https://github.com/{repository}/commit/{commit['sha']}"


HTML_TEMPLATE = """<!doctype html>
<html lang=\"pt-BR\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>PDF-printer — Captura técnica</title>
  <style>
    :root { color-scheme: light; --ink:#18212f; --muted:#5d6879; --accent:#0b67c2; --line:#d9e0e9; --soft:#f4f8fc; --danger:#a31d2c; }
    * { box-sizing:border-box; } body { margin:0; color:var(--ink); background:#eef3f8; font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }
    main { width:min(900px,calc(100% - 32px)); margin:32px auto 48px; } h1 { margin:0 0 8px; font-size:clamp(1.35rem,3vw,1.75rem); } .lead { margin:0 0 28px; color:var(--muted); }
    .github-header { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:28px; padding:12px 15px; background:#24292f; color:#fff; border-radius:11px; } .github-brand,.github-account { display:flex; align-items:center; gap:9px; } .github-brand { font-size:1.05rem; font-weight:750; } .github-mark { width:25px; height:25px; fill:currentColor; } .github-account { flex-wrap:wrap; justify-content:flex-end; color:#d0d7de; font-size:.9rem; } .github-account a { color:#fff; font-weight:700; text-decoration:none; } .account-dot { width:8px; height:8px; border-radius:50%; background:#3fb950; } .account-dot.off { background:#8c959f; } .github-button { margin:0; padding:7px 10px; background:#57606a; font-size:.8rem; } .github-button:hover { background:#6e7781; } .github-login-status { width:100%; color:#d0d7de; text-align:right; font-size:.78rem; }
    .card { background:#fff; border:1px solid var(--line); border-radius:14px; padding:28px; box-shadow:0 10px 30px #35516e14; } label { display:block; font-weight:650; margin:18px 0 7px; } label:first-child { margin-top:0; }
    input { width:100%; border:1px solid #aab8c8; border-radius:8px; font:inherit; padding:11px 12px; } input:focus { outline:3px solid #bcdcff; border-color:var(--accent); }
    .grid { display:grid; grid-template-columns:2fr 1fr; gap:16px; } .advanced { margin-top:24px; padding-top:4px; border-top:1px solid var(--line); }
    .toggle { display:flex; gap:9px; align-items:center; font-weight:650; margin:18px 0 5px; } .toggle input { width:auto; accent-color:var(--accent); } .hint { margin:6px 0 0; color:var(--muted); font-size:.9rem; }
    button { margin-top:24px; border:0; border-radius:8px; color:white; background:var(--accent); padding:12px 18px; font:700 16px system-ui; cursor:pointer; } button[disabled] { cursor:wait; opacity:.65; }
    #result { margin-top:24px; border-radius:10px; padding:15px 17px; display:none; } #result.ok { display:block; background:#e9f7ef; border:1px solid #a9dbba; } #result.error { display:block; color:#731722; background:#fff0f1; border:1px solid #f0b9be; } #result a { color:#0758a8; font-weight:650; }
    .notice { margin-top:24px; color:#4e5e70; font-size:.9rem; } code { background:#e9eef4; padding:2px 5px; border-radius:4px; }
    @media(max-width:600px) { main { margin:20px auto; } .github-header { align-items:flex-start; flex-direction:column; } .github-account { justify-content:flex-start; } .github-login-status { text-align:left; } .card { padding:20px; } .grid { grid-template-columns:1fr; gap:0; } }
  </style>
</head>
<body><main>
  <header class="github-header">
    <div class="github-brand" aria-label="GitHub">
      <svg class="github-mark" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0a8 8 0 0 0-2.53 15.59c.4.07.55-.17.55-.38l-.01-1.49c-2.01.44-2.43-.85-2.43-.85-.33-.84-.8-1.06-.8-1.06-.55-.38.04-.37.04-.37.61.04.93.63.93.63.54.93 1.42.66 1.77.5.05-.4.21-.66.38-.81-1.61-.18-3.3-.81-3.3-3.59 0-.79.28-1.44.74-1.95-.07-.18-.32-.92.07-1.92 0 0 .6-.19 1.98.74A6.86 6.86 0 0 1 8 1.8c.61 0 1.23.08 1.81.24 1.37-.93 1.98-.74 1.98-.74.39 1 .14 1.74.07 1.92.46.51.74 1.16.74 1.95 0 2.79-1.7 3.4-3.31 3.58.21.18.39.52.39 1.05l-.01 1.55c0 .21.14.46.55.38A8 8 0 0 0 8 0Z"/></svg>
      <span>GitHub</span>
    </div>
    <div class="github-account">
      <span id="account-dot" class="account-dot{% if not github_account %} off{% endif %}"></span>
      <span id="github-account-label">{% if github_account %}Conectado como <a href="https://github.com/{{ github_account }}" target="_blank" rel="noopener">@{{ github_account }}</a>{% else %}Nenhuma conta conectada{% endif %}</span>
      <button id="github-login" class="github-button" type="button">Conectar/trocar conta</button>
      <span id="github-login-status" class="github-login-status" hidden></span>
    </div>
  </header>
  <h1>Captura técnica de página</h1>
  <p class=\"lead\">Gera PDF com a página integral paginada, anexos verificáveis e captura do player no tempo indicado em links do YouTube.</p>
  <form id=\"capture-form\" class=\"card\">
    <label for=\"url\">Link da página</label>
    <input id=\"url\" name=\"url\" type=\"url\" placeholder=\"https://exemplo.com ou https://youtu.be/...?...\" required autofocus>
    <p class=\"hint\">Para YouTube, o tempo <code>?t=46m11s</code> ou <code>?t=2771</code> será aplicado e o vídeo será pausado nesse ponto.</p>
    <label for=\"label\">Nome opcional da captura</label>
    <input id=\"label\" name=\"label\" maxlength=\"70\" placeholder=\"Ex.: prova-video-audiencia\">
    <div class=\"advanced\">
      <label class=\"toggle\"><input id=\"publish\" name=\"publish\" type=\"checkbox\"> Publicar os artefatos em um repositório público do GitHub</label>
      <p class=\"hint\">A senha/token não é gravada. Use a conta exibida no cabeçalho ou informe um token fine-grained com <em>Contents: Read and write</em>.</p>
      <div id=\"github-fields\" hidden>
        <div class=\"grid\">
          <div><label for=\"repository\">Repositório público</label><input id=\"repository\" name=\"repository\" value=\"{{ github_repository }}\" placeholder=\"usuario/capturas-provas\"></div>
          <div><label for=\"branch\">Branch</label><input id=\"branch\" name=\"branch\" value=\"main\"></div>
        </div>
        <label for=\"github_token\">Token do GitHub (opcional)</label>
        <input id=\"github_token\" name=\"github_token\" type=\"password\" autocomplete=\"off\" placeholder=\"Deixe vazio para usar a conta conectada pelo GitHub CLI\">
      </div>
    </div>
    <button id=\"submit\" type=\"submit\">Gerar captura em PDF</button>
  </form>
  <p class=\"notice\">As capturas ficam em <code>capturas/</code>. Repositório público dá histórico e cópias externas, mas não substitui ata notarial nem divulgue dados sigilosos.</p>
  <section id=\"result\" role=\"status\"></section>
</main>
<script>
  const publish = document.querySelector('#publish'), fields = document.querySelector('#github-fields');
  publish.addEventListener('change', () => fields.hidden = !publish.checked);
  const githubButton = document.querySelector('#github-login'), githubLabel = document.querySelector('#github-account-label'), githubDot = document.querySelector('#account-dot'), githubStatus = document.querySelector('#github-login-status');
  function showGithubAccount(state) {
    githubLabel.replaceChildren();
    if (state.account) {
      githubDot.classList.remove('off');
      githubLabel.append('Conectado como ');
      const link = document.createElement('a'); link.href = `https://github.com/${encodeURIComponent(state.account)}`; link.target = '_blank'; link.rel = 'noopener'; link.textContent = `@${state.account}`; githubLabel.append(link);
    } else { githubDot.classList.add('off'); githubLabel.textContent = 'Nenhuma conta conectada'; }
  }
  async function refreshGithubLogin() {
    const response = await fetch('/api/github/auth'); const state = await response.json(); showGithubAccount(state);
    if (state.running) { githubStatus.hidden = false; githubStatus.textContent = state.device_code ? `${state.message} Código: ${state.device_code}` : state.message; window.setTimeout(refreshGithubLogin, 1800); }
    else if (githubStatus.hidden === false) { githubStatus.textContent = state.message; window.setTimeout(() => { githubStatus.hidden = true; }, 6000); }
  }
  githubButton.addEventListener('click', async () => {
    githubButton.disabled = true; githubStatus.hidden = false; githubStatus.textContent = 'Iniciando autorização…';
    try { await fetch('/api/github/login', { method:'POST' }); await refreshGithubLogin(); }
    catch (_) { githubStatus.textContent = 'Não foi possível iniciar a conexão com o GitHub.'; }
    finally { window.setTimeout(() => { githubButton.disabled = false; }, 1500); }
  });
  const form = document.querySelector('#capture-form'), submit = document.querySelector('#submit'), result = document.querySelector('#result');
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); submit.disabled = true; submit.textContent = 'Capturando página…'; result.className = ''; result.style.display = 'none';
    try {
      const response = await fetch('/api/captures', { method:'POST', body:new FormData(form) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Falha ao gerar a captura.');
      let html = `<strong>Captura concluída.</strong><br><a href="${data.pdf_url}">Baixar PDF consolidado</a> · <a href="${data.folder_url}">Abrir pasta dos artefatos</a>`;
      if (data.github_commit_url) html += `<br><a href="${data.github_commit_url}" target="_blank" rel="noopener">Ver commit de preservação no GitHub</a>`;
      if (data.github_warning) html += `<br><small>Captura local preservada. GitHub: ${data.github_warning}</small>`;
      result.innerHTML = html; result.className = 'ok';
    } catch (error) { result.textContent = error.message; result.className = 'error'; }
    finally { submit.disabled = false; submit.textContent = 'Gerar captura em PDF'; }
  });
</script></body></html>"""


def make_app(output_root: Path = DEFAULT_OUTPUT_DIR) -> Flask:
    application = Flask(__name__)
    output_root = output_root.resolve()

    @application.get("/")
    def index() -> str:
        return render_template_string(
            HTML_TEMPLATE,
            github_repository=connected_github_repository(),
            github_account=github_cli_account(),
        )

    @application.get("/api/github/auth")
    def api_github_auth():
        return jsonify(github_auth_status())

    @application.post("/api/github/login")
    def api_github_login():
        return jsonify(start_github_login())

    @application.post("/api/captures")
    def api_capture():
        if not CAPTURE_LOCK.acquire(blocking=False):
            return jsonify(error="Já há uma captura em andamento. Aguarde a conclusão."), 409
        try:
            result = capture_url(
                request.form.get("url", ""),
                output_root,
                request.form.get("label", ""),
            )
            if request.form.get("publish") == "on":
                try:
                    result.github_commit_url = publish_capture_to_github(
                        result,
                        request.form.get("repository", ""),
                        request.form.get("branch", DEFAULT_BRANCH),
                        request.form.get("github_token", ""),
                    )
                except GitHubPublishError as error:
                    result.github_warning = str(error)
            return jsonify(
                pdf_url=url_for("download_file", capture_id=result.capture_id, filename=result.pdf_path.name),
                folder_url=url_for("list_files", capture_id=result.capture_id),
                github_commit_url=result.github_commit_url,
                github_warning=result.github_warning,
            )
        except CaptureError as error:
            return jsonify(error=str(error)), 400
        finally:
            CAPTURE_LOCK.release()

    @application.get("/capturas/<capture_id>")
    def list_files(capture_id: str):
        folder = (output_root / capture_id).resolve()
        if folder.parent != output_root or not folder.is_dir():
            return "Captura não encontrada.", 404
        links = "".join(
            f'<li><a href="{url_for("download_file", capture_id=capture_id, filename=file.name)}">{file.name}</a></li>'
            for file in sorted(folder.iterdir())
            if file.is_file()
        )
        return f"<h1>{capture_id}</h1><ul>{links}</ul><p><a href='/'>Nova captura</a></p>"

    @application.get("/capturas/<capture_id>/<path:filename>")
    def download_file(capture_id: str, filename: str):
        folder = (output_root / capture_id).resolve()
        if folder.parent != output_root or not folder.is_dir():
            return "Captura não encontrada.", 404
        return send_from_directory(folder, filename, as_attachment=True)

    return application


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Captura páginas web em PDF técnico.")
    parser.add_argument("--url", help="URL a capturar. Sem esta opção, inicia a interface local.")
    parser.add_argument("--name", default="", help="Nome opcional da captura.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Diretório das capturas.")
    parser.add_argument("--repo", help="Repositório público para publicar, no formato usuario/repositorio.")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="Branch do repositório (padrão: main).")
    parser.add_argument("--host", default="127.0.0.1", help="Host da interface local.")
    parser.add_argument("--port", type=int, default=8765, help="Porta da interface local.")
    parser.add_argument("--no-browser", action="store_true", help="Não abre o navegador ao iniciar a interface.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.url:
        try:
            result = capture_url(args.url, args.output_dir, args.name)
            if args.repo:
                result.github_commit_url = publish_capture_to_github(result, args.repo, args.branch)
            print(json.dumps(
                {
                    "pdf": str(result.pdf_path),
                    "metadata": str(result.metadata_path),
                    "integrity": str(result.integrity_path),
                    "github_commit": result.github_commit_url,
                },
                ensure_ascii=False,
                indent=2,
            ))
            return 0
        except CaptureError as error:
            print(f"ERRO: {error}", file=sys.stderr)
            return 1

    application = make_app(args.output_dir)
    address = f"http://{args.host}:{args.port}"
    print(f"Interface disponível em {address}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(address)).start()
    application.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
