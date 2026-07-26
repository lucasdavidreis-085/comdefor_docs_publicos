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
import tempfile
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

from flask import Flask, jsonify, render_template_string, request, send_file, send_from_directory, url_for
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
) -> Any:
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


def github_cli_logout() -> bool:
    """Remove a sessão ativa somente depois de o usuário confirmar o comando na interface."""
    account = github_cli_account()
    if not account:
        return True
    try:
        subprocess.run(
            ["gh", "auth", "logout", "--hostname", "github.com", "--user", account],
            capture_output=True,
            check=True,
            text=True,
            timeout=20,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def github_repository_name(value: str) -> str:
    repository = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise GitHubPublishError("Escolha um repositório válido no GitHub.")
    return repository


def available_github_repositories() -> list[dict[str, Any]]:
    """Lista até 100 repositórios aos quais a conta ativa pode gravar ou visualizar."""
    token = github_cli_token()
    if not token:
        return []
    payload = github_api_request(
        "GET",
        "/user/repos?affiliation=owner%2Ccollaborator%2Corganization_member&sort=updated&per_page=100",
        token,
    )
    if not isinstance(payload, list):
        return []
    repositories = []
    for item in payload:
        name = item.get("full_name", "")
        can_write = bool((item.get("permissions") or {}).get("push"))
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) and not item.get("archived") and can_write:
            repositories.append(
                {
                    "full_name": name,
                    "private": bool(item.get("private")),
                    "description": item.get("description") or "",
                    "default_branch": item.get("default_branch") or DEFAULT_BRANCH,
                    "html_url": item.get("html_url") or f"https://github.com/{name}",
                }
            )
    return repositories


def github_documents(repository: str) -> list[dict[str, str]]:
    """Retorna os PDFs de captura já preservados em capturas/ no repositório selecionado."""
    repository = github_repository_name(repository)
    token = github_cli_token()
    if not token:
        raise GitHubPublishError("Conecte uma conta do GitHub para consultar os documentos.")
    repo_info = github_api_request("GET", f"/repos/{repository}", token)
    branch = repo_info.get("default_branch") or DEFAULT_BRANCH
    branch_ref = urllib.parse.quote(branch, safe="")
    tree = github_api_request(
        "GET",
        f"/repos/{repository}/git/trees/{branch_ref}?recursive=1",
        token,
    )
    documents = []
    for item in tree.get("tree", []):
        path = item.get("path", "")
        if item.get("type") == "blob" and re.fullmatch(r"capturas/[^/]+/captura_consolidada\.pdf", path):
            capture_id = path.split("/")[1]
            documents.append({"entry_id": capture_id, "path": path, "name": "Captura em PDF", "kind": "Captura"})
        elif item.get("type") == "blob" and re.fullmatch(r"documentos_enviados/[^/]+/[^/]+", path):
            upload_id, filename = path.split("/")[1:]
            if filename != "metadados_upload.json":
                documents.append({"entry_id": upload_id, "path": path, "name": filename, "kind": "Arquivo enviado"})
    return sorted(documents, key=lambda item: item["entry_id"], reverse=True)


def github_document_bytes(repository: str, path: str) -> bytes:
    repository = github_repository_name(repository)
    is_capture = re.fullmatch(r"capturas/[^/]+/captura_consolidada\.pdf", path)
    is_upload = re.fullmatch(r"documentos_enviados/[^/]+/[^/]+", path) and not path.endswith("/metadados_upload.json")
    if not (is_capture or is_upload):
        raise GitHubPublishError("O documento solicitado não é um arquivo válido.")
    token = github_cli_token()
    if not token:
        raise GitHubPublishError("Conecte uma conta do GitHub para baixar o documento.")
    api_path = f"/repos/{repository}/contents/{urllib.parse.quote(path, safe='/')}"
    request_object = urllib.request.Request(
        f"https://api.github.com{api_path}",
        headers={
            "Accept": "application/vnd.github.raw+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PDF-printer",
        },
    )
    try:
        with urllib.request.urlopen(request_object, timeout=90) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise GitHubPublishError(f"GitHub respondeu HTTP {error.code} ao baixar o documento.") from error
    except urllib.error.URLError as error:
        raise GitHubPublishError(f"Não foi possível baixar o documento: {error.reason}") from error


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


def publish_files_to_github(
    repository: str,
    branch: str,
    files: list[tuple[str, bytes]],
    message: str,
    token: str | None = None,
) -> str:
    """Cria um commit atômico com os arquivos informados no repositório escolhido."""
    repository = github_repository_name(repository)
    branch = branch.strip() or DEFAULT_BRANCH
    token = (token or os.environ.get("GITHUB_TOKEN") or github_cli_token()).strip()
    if not token:
        raise GitHubPublishError(
            "Informe um token do GitHub com permissão Contents: Read and write, "
            "defina GITHUB_TOKEN ou conecte-se com 'gh auth login'."
        )

    github_api_request("GET", f"/repos/{repository}", token)
    ref_path = urllib.parse.quote(branch, safe="")
    ref = github_api_request("GET", f"/repos/{repository}/git/ref/heads/{ref_path}", token)
    parent_commit = ref["object"]["sha"]
    commit_info = github_api_request("GET", f"/repos/{repository}/git/commits/{parent_commit}", token)
    base_tree = commit_info["tree"]["sha"]

    oversized = [path for path, data in files if len(data) > MAX_GITHUB_FILE_BYTES]
    if oversized:
        raise GitHubPublishError(
            "O GitHub não aceita um ou mais arquivos acima de 95 MiB: " + ", ".join(oversized)
        )

    tree_entries: list[dict[str, str]] = []
    for file_path, data in files:
        blob = github_api_request(
            "POST",
            f"/repos/{repository}/git/blobs",
            token,
            {
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
            },
        )
        tree_entries.append(
            {
                "path": file_path,
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
    commit = github_api_request(
        "POST",
        f"/repos/{repository}/git/commits",
        token,
        {
            "message": message[:250],
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


def publish_capture_to_github(
    result: CaptureResult,
    repository: str,
    branch: str = DEFAULT_BRANCH,
    token: str | None = None,
    folder: str = "",
) -> str:
    base_path = repository_join_path(folder, "capturas", result.capture_id)
    files = [
        (f"{base_path}/{file_path.name}", file_path.read_bytes())
        for file_path in sorted(path for path in result.output_dir.iterdir() if path.is_file())
    ]
    label = result.title or result.capture_id
    return publish_files_to_github(
        repository,
        branch,
        files,
        f"Preserva captura: {label}",
        token,
    )


def safe_upload_filename(value: str) -> str:
    name = Path(value).name.strip()
    if not name or name in {".", ".."}:
        raise GitHubPublishError("Escolha um arquivo com nome válido.")
    stem = slugify(Path(name).stem, fallback="arquivo")
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(name).suffix)[:20]
    return f"{stem}{suffix.lower()}"


def publish_uploaded_file(
    repository: str,
    branch: str,
    original_name: str,
    content_type: str,
    data: bytes,
    folder: str = "",
) -> str:
    if not data:
        raise GitHubPublishError("O arquivo selecionado está vazio.")
    if len(data) > MAX_GITHUB_FILE_BYTES:
        raise GitHubPublishError("O arquivo excede o limite de 95 MiB para envio ao GitHub.")
    filename = safe_upload_filename(original_name)
    now = datetime.now(timezone.utc)
    repository = github_repository_name(repository)
    branch = branch.strip() or DEFAULT_BRANCH
    digest = sha256_bytes(data)
    upload_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}_{slugify(Path(filename).stem)}_{digest[:12]}"
    base_path = repository_join_path(folder, "documentos_enviados", upload_id)
    metadata = {
        "versao_formato": "1.0",
        "nome_original": original_name,
        "arquivo_preservado": filename,
        "tipo_mime_informado": content_type or "application/octet-stream",
        "tamanho_bytes": len(data),
        "sha256_arquivo": digest,
        "data_hora_upload_utc": now.isoformat(timespec="seconds"),
        "origem": "Envio direto pelo PDF-printer",
        "repositorio_github": repository,
        "ramo_github": branch,
        "caminho_github": f"{base_path}/{filename}",
    }
    return publish_files_to_github(
        repository,
        branch,
        [
            (f"{base_path}/{filename}", data),
            (f"{base_path}/metadados_upload.json", json_bytes(metadata)),
        ],
        f"Preserva arquivo enviado: {filename}",
    )


def normalize_repository_path(value: str, *, allow_root: bool = True) -> str:
    """Normaliza um caminho relativo do repositório, sem aceitar travessia de diretórios."""
    raw_path = value.strip().replace("\\", "/").strip("/")
    if not raw_path:
        if allow_root:
            return ""
        raise GitHubPublishError("Informe uma pasta ou arquivo válido.")
    parts = raw_path.split("/")
    if any(
        not part
        or part in {".", ".."}
        or "\x00" in part
        or any(ord(character) < 32 for character in part)
        for part in parts
    ):
        raise GitHubPublishError("O caminho informado não é válido.")
    return "/".join(parts)


def repository_join_path(*parts: str) -> str:
    return "/".join(
        normalized
        for part in parts
        if (normalized := normalize_repository_path(part, allow_root=True))
    )


def repository_filename(value: str) -> str:
    filename = value.strip()
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        raise GitHubPublishError("Informe um nome de arquivo válido.")
    return filename[:180]


def github_repository_tree(
    repository: str,
    branch: str = "",
    token: str | None = None,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    """Obtém a árvore completa do ramo usado pela interface."""
    repository = github_repository_name(repository)
    token = (token or github_cli_token()).strip()
    if not token:
        raise GitHubPublishError("Conecte uma conta do GitHub para acessar o repositório.")
    repo_info = github_api_request("GET", f"/repos/{repository}", token)
    selected_branch = branch.strip() or repo_info.get("default_branch") or DEFAULT_BRANCH
    tree_ref = urllib.parse.quote(selected_branch, safe="")
    tree = github_api_request(
        "GET",
        f"/repos/{repository}/git/trees/{tree_ref}?recursive=1",
        token,
    )
    if tree.get("truncated"):
        raise GitHubPublishError("O repositório tem arquivos demais para a listagem completa.")
    entries = [entry for entry in tree.get("tree", []) if isinstance(entry, dict)]
    return repository, selected_branch, token, entries


def github_raw_file_bytes(repository: str, path: str, token: str) -> bytes:
    path = normalize_repository_path(path, allow_root=False)
    api_path = f"/repos/{repository}/contents/{urllib.parse.quote(path, safe='/')}"
    request_object = urllib.request.Request(
        f"https://api.github.com{api_path}",
        headers={
            "Accept": "application/vnd.github.raw+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PDF-printer",
        },
    )
    try:
        with urllib.request.urlopen(request_object, timeout=90) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise GitHubPublishError(f"GitHub respondeu HTTP {error.code} ao baixar o arquivo.") from error
    except urllib.error.URLError as error:
        raise GitHubPublishError(f"Não foi possível baixar o arquivo: {error.reason}") from error


def record_support_paths(path: str, all_paths: set[str]) -> tuple[str, str, str]:
    """Localiza metadados e hash associados aos registros produzidos pelo aplicativo."""
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    prefix = f"{parent}/" if parent else ""
    capture_metadata = f"{prefix}metadados_captura.json"
    upload_metadata = f"{prefix}metadados_upload.json"
    integrity = f"{prefix}integridade.sha256"
    if capture_metadata in all_paths:
        return "Captura", capture_metadata, integrity if integrity in all_paths else ""
    if upload_metadata in all_paths:
        return "Arquivo enviado", upload_metadata, ""
    return "Arquivo", "", ""


def is_record_support_file(path: str, all_paths: set[str]) -> bool:
    filename = path.rsplit("/", 1)[-1]
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    prefix = f"{parent}/" if parent else ""
    is_record = (
        f"{prefix}metadados_captura.json" in all_paths
        or f"{prefix}metadados_upload.json" in all_paths
    )
    if filename == ".gitkeep":
        return True
    return is_record and filename in {
        "metadados_captura.json",
        "metadados_upload.json",
        "integridade.sha256",
        "pagina_original.html",
        "pagina_integral.png",
        "player_ou_viewport.png",
    }


def github_repository_contents(repository: str, folder: str = "") -> dict[str, Any]:
    """Lista a pasta pedida e todas as pastas disponíveis para os campos de destino."""
    folder = normalize_repository_path(folder, allow_root=True)
    repository, branch, _token, entries = github_repository_tree(repository)
    all_paths = {entry.get("path", "") for entry in entries if entry.get("path")}
    prefix = f"{folder}/" if folder else ""
    folders: dict[str, dict[str, str]] = {}
    files: list[dict[str, Any]] = []
    all_folders: set[str] = set()

    for entry in entries:
        path = entry.get("path", "")
        if not path or not isinstance(path, str):
            continue
        components = path.split("/")
        for index in range(1, len(components)):
            all_folders.add("/".join(components[:index]))
        if not path.startswith(prefix):
            continue
        remaining = path[len(prefix):]
        if not remaining:
            continue
        if "/" in remaining:
            name = remaining.split("/", 1)[0]
            child_path = f"{prefix}{name}"
            folders[child_path] = {"name": name, "path": child_path}
            continue
        if entry.get("type") != "blob" or is_record_support_file(path, all_paths):
            continue
        kind, metadata_path, integrity_path = record_support_paths(path, all_paths)
        files.append(
            {
                "name": remaining,
                "path": path,
                "size": int(entry.get("size") or 0),
                "kind": kind,
                "metadata_path": metadata_path,
                "integrity_path": integrity_path,
            }
        )

    return {
        "repository": repository,
        "branch": branch,
        "path": folder,
        "parent": folder.rsplit("/", 1)[0] if "/" in folder else "",
        "folders": sorted(folders.values(), key=lambda item: item["name"].lower()),
        "all_folders": sorted(all_folders, key=str.lower),
        "files": sorted(files, key=lambda item: item["name"].lower()),
    }


def github_document_bytes(repository: str, path: str) -> bytes:
    repository = github_repository_name(repository)
    path = normalize_repository_path(path, allow_root=False)
    token = github_cli_token()
    if not token:
        raise GitHubPublishError("Conecte uma conta do GitHub para baixar o arquivo.")
    return github_raw_file_bytes(repository, path, token)


def github_commit_tree_entries(
    repository: str,
    branch: str,
    tree_entries: list[dict[str, Any]],
    message: str,
    token: str | None = None,
) -> str:
    """Cria um único commit para alterações já representadas como entradas Git."""
    repository = github_repository_name(repository)
    token = (token or github_cli_token()).strip()
    if not token:
        raise GitHubPublishError("Conecte uma conta do GitHub para gravar no repositório.")
    repo_info = github_api_request("GET", f"/repos/{repository}", token)
    branch = branch.strip() or repo_info.get("default_branch") or DEFAULT_BRANCH
    ref_path = urllib.parse.quote(branch, safe="")
    ref = github_api_request("GET", f"/repos/{repository}/git/ref/heads/{ref_path}", token)
    parent_commit = ref["object"]["sha"]
    commit_info = github_api_request("GET", f"/repos/{repository}/git/commits/{parent_commit}", token)
    tree = github_api_request(
        "POST",
        f"/repos/{repository}/git/trees",
        token,
        {"base_tree": commit_info["tree"]["sha"], "tree": tree_entries},
    )
    commit = github_api_request(
        "POST",
        f"/repos/{repository}/git/commits",
        token,
        {"message": message[:250], "tree": tree["sha"], "parents": [parent_commit]},
    )
    github_api_request(
        "PATCH",
        f"/repos/{repository}/git/refs/heads/{ref_path}",
        token,
        {"sha": commit["sha"], "force": False},
    )
    return f"https://github.com/{repository}/commit/{commit['sha']}"


def create_github_blob(repository: str, data: bytes, token: str) -> str:
    blob = github_api_request(
        "POST",
        f"/repos/{repository}/git/blobs",
        token,
        {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"},
    )
    return blob["sha"]


def create_github_folder(repository: str, branch: str, folder: str) -> str:
    folder = normalize_repository_path(folder, allow_root=False)
    _repository, _branch, token, entries = github_repository_tree(repository, branch)
    placeholder = f"{folder}/.gitkeep"
    if any(
        entry_path == folder or entry_path.startswith(f"{folder}/")
        for entry in entries
        if (entry_path := entry.get("path", ""))
    ):
        raise GitHubPublishError("Esta pasta já existe.")
    return publish_files_to_github(
        repository,
        branch,
        [(placeholder, b"")],
        f"Cria pasta: {folder}",
        token,
    )


def rename_github_file(repository: str, branch: str, path: str, new_name: str) -> str:
    repository = github_repository_name(repository)
    path = normalize_repository_path(path, allow_root=False)
    new_name = repository_filename(new_name)
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    new_path = repository_join_path(parent, new_name)
    if new_path == path:
        return ""
    _repository, resolved_branch, token, entries = github_repository_tree(repository, branch)
    all_paths = {entry.get("path", "") for entry in entries if entry.get("path")}
    source = next((entry for entry in entries if entry.get("path") == path and entry.get("type") == "blob"), None)
    if source is None:
        raise GitHubPublishError("O arquivo não foi encontrado no repositório.")
    if new_path in all_paths:
        raise GitHubPublishError("Já existe um arquivo com este nome nesta pasta.")
    changes: list[dict[str, Any]] = [
        {"path": new_path, "mode": source.get("mode") or "100644", "type": "blob", "sha": source["sha"]},
        {"path": path, "mode": "100644", "type": "blob", "sha": None},
    ]
    _kind, metadata_path, _integrity_path = record_support_paths(path, all_paths)
    if metadata_path:
        try:
            metadata = json.loads(github_raw_file_bytes(repository, metadata_path, token).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, dict):
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            history = metadata.get("historico_alteracoes")
            if not isinstance(history, list):
                history = []
            history.append({"data_hora_utc": now, "arquivo_anterior": path, "arquivo_atual": new_path})
            metadata.update(
                {
                    "arquivo_preservado": new_name,
                    "caminho_github": new_path,
                    "data_hora_ultima_alteracao_utc": now,
                    "historico_alteracoes": history[-20:],
                }
            )
            changes.append(
                {
                    "path": metadata_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": create_github_blob(repository, json_bytes(metadata), token),
                }
            )
    return github_commit_tree_entries(
        repository,
        resolved_branch,
        changes,
        f"Renomeia arquivo: {Path(path).name} para {new_name}",
        token,
    )


def github_last_change(repository: str, branch: str, path: str, token: str) -> dict[str, str]:
    query = urllib.parse.urlencode({"sha": branch, "path": path, "per_page": 1})
    commits = github_api_request("GET", f"/repos/{repository}/commits?{query}", token)
    if not isinstance(commits, list) or not commits:
        return {}
    item = commits[0]
    commit = item.get("commit") or {}
    author = commit.get("author") or {}
    return {
        "data_hora_ultima_alteracao_utc": author.get("date") or "não informada",
        "autor_ultima_alteracao": author.get("name") or "não informado",
        "commit": item.get("sha") or "",
        "url_commit": item.get("html_url") or "",
    }


def github_record_pdf(repository: str, path: str) -> bytes:
    repository = github_repository_name(repository)
    path = normalize_repository_path(path, allow_root=False)
    _repository, branch, token, entries = github_repository_tree(repository)
    all_paths = {entry.get("path", "") for entry in entries if entry.get("path")}
    if path not in all_paths:
        raise GitHubPublishError("O arquivo não foi encontrado no repositório.")
    file_data = github_raw_file_bytes(repository, path, token)
    kind, metadata_path, integrity_path = record_support_paths(path, all_paths)
    metadata: dict[str, Any] = {}
    if metadata_path:
        try:
            value = json.loads(github_raw_file_bytes(repository, metadata_path, token).decode("utf-8"))
            metadata = value if isinstance(value, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            metadata = {"leitura_metadados": "Não foi possível interpretar o JSON associado."}
    last_change = github_last_change(repository, branch, path, token)
    rows: list[tuple[str, str]] = [
        ("Arquivo", Path(path).name),
        ("Tipo de registro", kind),
        ("Repositório", repository),
        ("Ramo", branch),
        ("Caminho no GitHub", path),
        ("SHA-256 atual do arquivo", sha256_bytes(file_data)),
        ("Tamanho atual", f"{len(file_data)} bytes"),
        ("Registro PDF gerado em UTC", datetime.now(timezone.utc).isoformat(timespec="seconds")),
    ]
    for label, value in last_change.items():
        if value:
            rows.append((label.replace("_", " ").capitalize(), str(value)))
    if integrity_path:
        rows.append(("Arquivo de integridade", integrity_path))
    for key in (
        "data_hora_upload_utc",
        "data_hora_captura_utc",
        "data_hora_ultima_alteracao_utc",
        "tipo_mime_informado",
        "sha256_arquivo",
        "url_informada",
        "url_final_acessada",
    ):
        if metadata.get(key) not in {None, ""}:
            rows.append((key.replace("_", " ").capitalize(), str(metadata[key])))

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4
    margin, y = 1.45 * cm, page_height - (1.45 * cm)
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(margin, y, "REGISTRO DE PRESERVAÇÃO NO GITHUB")
    y -= 0.8 * cm
    for label, value in rows:
        lines = [f"{label}:"] + [f"  {line}" for line in _wrap_text(value, 96)] + [""]
        for line in lines:
            if y < 1.7 * cm:
                pdf.showPage()
                y = page_height - (1.45 * cm)
            pdf.setFont("Helvetica-Bold" if line == f"{label}:" else "Helvetica", 8.6)
            pdf.drawString(margin, y, line)
            y -= 0.42 * cm
    pdf.save()
    return buffer.getvalue()


LEGACY_HTML_TEMPLATE = """<!doctype html>
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
    #result,#upload-result { margin-top:18px; border-radius:10px; padding:15px 17px; display:none; } #result.ok,#upload-result.ok { display:block; background:#e9f7ef; border:1px solid #a9dbba; } #result.error,#upload-result.error { display:block; color:#731722; background:#fff0f1; border:1px solid #f0b9be; } #result a,#upload-result a { color:#0758a8; font-weight:650; }
    .notice { margin-top:24px; color:#4e5e70; font-size:.9rem; } code { background:#e9eef4; padding:2px 5px; border-radius:4px; }
    .danger-button { margin:0; padding:7px 10px; color:#ffb4b4; background:transparent; border:1px solid #6e3b3b; font-size:.8rem; } .panel-head { display:flex; align-items:center; justify-content:space-between; gap:12px; } .panel-head h2 { margin:0; font-size:1.05rem; } .minor-button { margin:0; padding:7px 10px; color:#253247; background:#e9eef5; font-size:.8rem; } .repository-list,.document-list { display:grid; gap:9px; margin-top:16px; max-height:310px; overflow:auto; padding-right:3px; } .repository { display:flex; align-items:center; justify-content:space-between; gap:12px; width:100%; margin:0; padding:11px 12px; color:var(--ink); background:#fff; border:1px solid var(--line); text-align:left; } .repository:hover,.repository.selected { border-color:var(--accent); background:#f0f7ff; } .repo-name { font-weight:750; } .repo-description { display:block; color:var(--muted); font-size:.82rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:560px; } .visibility { flex:0 0 auto; border-radius:99px; background:#ddf4ff; color:#0550ae; padding:2px 7px; font-size:.75rem; } .visibility.private { background:#fff1e5; color:#9a6700; } .selected-repository { margin:18px 0 0; padding:11px 12px; border-left:4px solid #8c959f; background:#f6f8fa; color:#4b5565; } .selected-repository.active { border-color:var(--accent); color:#172033; } .document { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 0; border-bottom:1px solid #eaeef2; } .document:last-child { border-bottom:0; } .document small { display:block; color:var(--muted); } .download { color:#0550ae; font-weight:700; text-decoration:none; white-space:nowrap; }
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
      <button id="github-logout" class="danger-button" type="button"{% if not github_account %} hidden{% endif %}>Sair</button>
      <span id="github-login-status" class="github-login-status" hidden></span>
    </div>
  </header>
  <h1>Captura técnica de página</h1>
  <p class=\"lead\">Escolha o repositório de destino, gere a captura e acesse os PDFs preservados diretamente nesta página.</p>
  <section class=\"card\">
    <div class=\"panel-head\"><div><h2>Repositórios disponíveis</h2><p class=\"hint\">Repositórios aos quais a conta conectada tem acesso.</p></div><button id=\"refresh-repositories\" class=\"minor-button\" type=\"button\">Atualizar</button></div>
    <div id=\"repository-list\" class=\"repository-list\"><p class=\"hint\">Conecte uma conta para listar os repositórios.</p></div>
    <p id=\"selected-repository\" class=\"selected-repository\">Nenhum repositório selecionado.</p>
    <button id=\"clear-repository\" class=\"minor-button\" type=\"button\" hidden>Limpar seleção</button>
  </section>
  <section id=\"documents-panel\" class=\"card\" hidden>
    <div class=\"panel-head\"><div><h2>Documentos preservados</h2><p id=\"documents-note\" class=\"hint\"></p></div><a id=\"repository-link\" class=\"download\" target=\"_blank\" rel=\"noopener\">Abrir repositório</a></div>
    <div id=\"document-list\" class=\"document-list\"></div>
  </section>
  <form id=\"capture-form\" class=\"card\">
    <input id=\"selected-repository-input\" name=\"repository\" type=\"hidden\"><input id=\"selected-branch-input\" name=\"branch\" type=\"hidden\">
    <label for=\"url\">Link da página</label>
    <input id=\"url\" name=\"url\" type=\"url\" placeholder=\"https://exemplo.com ou https://youtu.be/...?...\" required autofocus>
    <p class=\"hint\">Para YouTube, o tempo <code>?t=46m11s</code> ou <code>?t=2771</code> será aplicado e o vídeo será pausado nesse ponto.</p>
    <label for=\"label\">Nome opcional da captura</label>
    <input id=\"label\" name=\"label\" maxlength=\"70\" placeholder=\"Ex.: prova-video-audiencia\">
    <div class=\"advanced\" hidden>
      <label class=\"toggle\"><input id=\"publish\" name=\"publish\" type=\"checkbox\"> Publicar os artefatos em um repositório público do GitHub</label>
      <p class=\"hint\">A senha/token não é gravada. Use a conta exibida no cabeçalho ou informe um token fine-grained com <em>Contents: Read and write</em>.</p>
      <div id=\"github-fields\" hidden>
        <div class=\"grid\">
          <div><label for=\"repository\">Repositório público</label><input id=\"repository\" name=\"legacy_repository\" value=\"{{ github_repository }}\" placeholder=\"usuario/capturas-provas\"></div>
          <div><label for=\"branch\">Branch</label><input id=\"branch\" name=\"legacy_branch\" value=\"main\"></div>
        </div>
        <label for=\"github_token\">Token do GitHub (opcional)</label>
        <input id=\"github_token\" name=\"github_token\" type=\"password\" autocomplete=\"off\" placeholder=\"Deixe vazio para usar a conta conectada pelo GitHub CLI\">
      </div>
    </div>
    <button id=\"submit\" type=\"submit\" disabled>Escolha um repositório para capturar</button>
  </form>
  <form id=\"upload-form\" class=\"card\" enctype=\"multipart/form-data\">
    <input id=\"upload-repository-input\" name=\"repository\" type=\"hidden\"><input id=\"upload-branch-input\" name=\"branch\" type=\"hidden\">
    <h2>Enviar arquivo do computador</h2>
    <p class=\"hint\">O arquivo original e um JSON com nome, tamanho, tipo, data e SHA-256 serão preservados no repositório selecionado.</p>
    <label for=\"file\">Arquivo</label>
    <input id=\"file\" name=\"file\" type=\"file\" required>
    <button id=\"upload-submit\" type=\"submit\" disabled>Escolha um repositório para enviar</button>
    <section id=\"upload-result\" role=\"status\"></section>
  </form>
  <p class=\"notice\">A captura é gerada em área temporária e enviada ao repositório escolhido; ela não permanece salva automaticamente no computador. Baixe o PDF pela lista de documentos quando quiser.</p>
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
</script>
<script>
(() => {
  const legacyForm = document.querySelector('#capture-form');
  const form = legacyForm.cloneNode(true); legacyForm.replaceWith(form);
  const githubButton = document.querySelector('#github-login'), githubLogout = document.querySelector('#github-logout'), githubLabel = document.querySelector('#github-account-label'), githubDot = document.querySelector('#account-dot'), githubStatus = document.querySelector('#github-login-status');
  const repositoryList = document.querySelector('#repository-list'), selectedLabel = document.querySelector('#selected-repository'), clearButton = document.querySelector('#clear-repository'), repositoryInput = form.querySelector('#selected-repository-input'), branchInput = form.querySelector('#selected-branch-input');
  const documentsPanel = document.querySelector('#documents-panel'), documentsNote = document.querySelector('#documents-note'), documentList = document.querySelector('#document-list'), repositoryLink = document.querySelector('#repository-link'), submit = form.querySelector('#submit'), result = document.querySelector('#result');
  const uploadForm = document.querySelector('#upload-form'), uploadRepositoryInput = uploadForm.querySelector('#upload-repository-input'), uploadBranchInput = uploadForm.querySelector('#upload-branch-input'), uploadSubmit = uploadForm.querySelector('#upload-submit'), uploadResult = uploadForm.querySelector('#upload-result');
  let repositories = [], selectedRepository = null;
  function message(target, text) { target.replaceChildren(); const item = document.createElement('p'); item.className = 'hint'; item.textContent = text; target.append(item); }
  async function api(url, options = {}) { const response = await fetch(url, options); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Não foi possível concluir a operação.'); return data; }
  function showGithubAccount(state) { githubLabel.replaceChildren(); if (state.account) { githubDot.classList.remove('off'); githubLabel.append('Conectado como '); const link = document.createElement('a'); link.href = `https://github.com/${encodeURIComponent(state.account)}`; link.target = '_blank'; link.rel = 'noopener'; link.textContent = `@${state.account}`; githubLabel.append(link); githubLogout.hidden = false; } else { githubDot.classList.add('off'); githubLabel.textContent = 'Nenhuma conta conectada'; githubLogout.hidden = true; } }
  function clearSelection() { selectedRepository = null; repositoryInput.value = ''; branchInput.value = ''; uploadRepositoryInput.value = ''; uploadBranchInput.value = ''; selectedLabel.textContent = 'Nenhum repositório selecionado.'; selectedLabel.classList.remove('active'); clearButton.hidden = true; documentsPanel.hidden = true; submit.disabled = true; submit.textContent = 'Escolha um repositório para capturar'; uploadSubmit.disabled = true; uploadSubmit.textContent = 'Escolha um repositório para enviar'; repositoryList.querySelectorAll('.repository').forEach(item => item.classList.remove('selected')); }
  function renderRepositories() { repositoryList.replaceChildren(); if (!repositories.length) { message(repositoryList, 'Nenhum repositório disponível para esta conta.'); return; } repositories.forEach(repo => { const button = document.createElement('button'); button.type = 'button'; button.className = 'repository'; button.dataset.repository = repo.full_name; const details = document.createElement('span'); const name = document.createElement('span'); name.className = 'repo-name'; name.textContent = repo.full_name; const description = document.createElement('span'); description.className = 'repo-description'; description.textContent = repo.description || 'Sem descrição'; details.append(name, description); const visibility = document.createElement('span'); visibility.className = `visibility${repo.private ? ' private' : ''}`; visibility.textContent = repo.private ? 'Privado' : 'Público'; button.append(details, visibility); button.addEventListener('click', () => chooseRepository(repo)); repositoryList.append(button); }); }
  async function loadRepositories() { message(repositoryList, 'Carregando repositórios…'); try { const data = await api('/api/github/repositories'); repositories = data.repositories; renderRepositories(); } catch (error) { message(repositoryList, error.message); } }
  async function loadDocuments(repo) { documentsPanel.hidden = false; documentsNote.textContent = `Documentos em ${repo.full_name}`; repositoryLink.href = repo.html_url; message(documentList, 'Carregando documentos…'); try { const data = await api(`/api/github/documents?repository=${encodeURIComponent(repo.full_name)}`); documentList.replaceChildren(); if (!data.documents.length) { message(documentList, 'Ainda não há documentos neste repositório.'); return; } data.documents.forEach(document => { const row = document.createElement('div'); row.className = 'document'; const details = document.createElement('span'); details.textContent = document.name; const metadata = document.createElement('small'); metadata.textContent = `${document.kind} - ${document.entry_id.replace('T', ' ').replace('Z', ' UTC')}`; details.append(metadata); const download = document.createElement('a'); download.className = 'download'; download.textContent = 'Baixar'; download.href = `/api/github/documents/download?repository=${encodeURIComponent(repo.full_name)}&path=${encodeURIComponent(document.path)}`; row.append(details, download); documentList.append(row); }); } catch (error) { message(documentList, error.message); } }
  function chooseRepository(repo) { selectedRepository = repo; repositoryInput.value = repo.full_name; branchInput.value = repo.default_branch; uploadRepositoryInput.value = repo.full_name; uploadBranchInput.value = repo.default_branch; selectedLabel.textContent = `Repositório selecionado: ${repo.full_name}`; selectedLabel.classList.add('active'); clearButton.hidden = false; submit.disabled = false; submit.textContent = 'Gerar captura e guardar no GitHub'; uploadSubmit.disabled = false; uploadSubmit.textContent = 'Enviar arquivo ao GitHub'; repositoryList.querySelectorAll('.repository').forEach(item => item.classList.toggle('selected', item.dataset.repository === repo.full_name)); loadDocuments(repo); }
  async function refreshGithubLogin() { const state = await api('/api/github/auth'); showGithubAccount(state); if (state.running) { githubStatus.hidden = false; githubStatus.textContent = state.device_code ? `${state.message} Código: ${state.device_code}` : state.message; window.setTimeout(refreshGithubLogin, 1800); } else { githubButton.disabled = false; if (!githubStatus.hidden) { githubStatus.textContent = state.message; window.setTimeout(() => { githubStatus.hidden = true; }, 6000); } if (state.account) loadRepositories(); } }
  githubButton.addEventListener('click', async () => { githubButton.disabled = true; githubStatus.hidden = false; githubStatus.textContent = 'Iniciando autorização…'; try { await api('/api/github/login', {method:'POST'}); await refreshGithubLogin(); } catch (error) { githubStatus.textContent = error.message; githubButton.disabled = false; } });
  githubLogout.addEventListener('click', async () => { if (!confirm('Sair da conta do GitHub neste aplicativo?')) return; try { await api('/api/github/logout', {method:'POST'}); showGithubAccount({account:''}); clearSelection(); repositories = []; message(repositoryList, 'Conecte uma conta para listar os repositórios.'); } catch (error) { alert(error.message); } });
  document.querySelector('#refresh-repositories').addEventListener('click', loadRepositories); clearButton.addEventListener('click', clearSelection);
  form.addEventListener('submit', async (event) => { event.preventDefault(); if (!selectedRepository) return; submit.disabled = true; submit.textContent = 'Gerando e enviando…'; result.className = ''; result.style.display = 'none'; try { const data = await api('/api/captures', {method:'POST', body:new FormData(form)}); result.replaceChildren(); const title = document.createElement('strong'); title.textContent = 'Captura concluída e preservada no GitHub.'; const link = document.createElement('a'); link.href = data.github_commit_url; link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Ver commit'; result.append(title, document.createElement('br'), link); result.className = 'ok'; await loadDocuments(selectedRepository); } catch (error) { result.textContent = error.message; result.className = 'error'; } finally { submit.disabled = false; submit.textContent = 'Gerar captura e guardar no GitHub'; } });
  uploadForm.addEventListener('submit', async (event) => { event.preventDefault(); if (!selectedRepository) return; const file = uploadForm.querySelector('#file').files[0]; if (!file) return; uploadSubmit.disabled = true; uploadSubmit.textContent = 'Enviando arquivo…'; uploadResult.className = ''; uploadResult.style.display = 'none'; try { const data = await api('/api/github/files', {method:'POST', body:new FormData(uploadForm)}); uploadResult.replaceChildren(); const title = document.createElement('strong'); title.textContent = 'Arquivo enviado e preservado no GitHub.'; const link = document.createElement('a'); link.href = data.github_commit_url; link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Ver commit'; uploadResult.append(title, document.createElement('br'), link); uploadResult.className = 'ok'; uploadForm.querySelector('#file').value = ''; await loadDocuments(selectedRepository); } catch (error) { uploadResult.textContent = error.message; uploadResult.className = 'error'; } finally { uploadSubmit.disabled = false; uploadSubmit.textContent = 'Enviar arquivo ao GitHub'; } });
  if (!githubDot.classList.contains('off')) loadRepositories();
})();
</script></body></html>"""


HTML_TEMPLATE = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PDF-printer</title>
  <style>
    :root { --ink:#172033; --muted:#647184; --line:#dce2e9; --accent:#0969da; --soft:#f6f8fa; --danger:#b42318; }
    * { box-sizing:border-box; } body { margin:0; background:#fff; color:var(--ink); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
    a { color:#0758a8; } button,input,select { font:inherit; } button { cursor:pointer; }
    .topbar { min-height:52px; padding:0 max(20px,calc((100% - 1120px)/2)); display:flex; align-items:center; justify-content:space-between; gap:18px; color:#f0f6fc; background:#24292f; }
    .brand,.account { display:flex; align-items:center; gap:8px; } .brand { font-weight:760; font-size:15px; } .mark { width:22px; height:22px; fill:currentColor; } .account { flex-wrap:wrap; justify-content:flex-end; font-size:13px; color:#d0d7de; } .account a { color:#fff; font-weight:700; }
    .dot { width:7px; height:7px; border-radius:50%; background:#3fb950; } .dot.off { background:#8c959f; } .topbar button { border:1px solid #57606a; color:#fff; background:transparent; padding:4px 8px; border-radius:3px; font-size:12px; } .topbar .logout { color:#ffb4b4; border-color:#6e3b3b; } #github-login-status { width:100%; text-align:right; color:#d0d7de; font-size:12px; }
    main { width:min(1120px,calc(100% - 32px)); margin:24px auto 40px; } .app-grid { display:grid; grid-template-columns:215px minmax(0,1fr); gap:30px; }
    aside { border-right:1px solid var(--line); padding-right:22px; min-height:560px; } .side-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:12px; } .side-head strong { font-size:12px; letter-spacing:.06em; } .text-button { border:0; padding:0; background:transparent; color:#0758a8; text-decoration:underline; font-size:12px; }
    #repository-list { display:grid; gap:12px; } .repo { min-width:0; } .repo a { display:inline-block; color:#172033; font-weight:700; text-decoration:underline; text-underline-offset:3px; overflow-wrap:anywhere; } .repo a:hover { color:#0758a8; } .repo .active-repo { display:inline-block; color:#0758a8; font-weight:800; overflow-wrap:anywhere; } .repo small { display:block; margin-top:1px; color:var(--muted); font-size:12px; }
    .workspace[hidden], .tab-panel[hidden] { display:none; } .workspace-head { display:flex; align-items:baseline; justify-content:space-between; gap:12px; padding-bottom:13px; border-bottom:1px solid var(--line); } .workspace-head strong { font-size:15px; } .workspace-head a { font-size:12px; }
    .tabs { display:flex; gap:20px; border-bottom:1px solid var(--line); margin-bottom:18px; } .tab { margin:0; padding:11px 0 9px; color:var(--muted); background:transparent; border:0; border-bottom:2px solid transparent; font-weight:750; font-size:12px; letter-spacing:.03em; } .tab:hover { color:var(--ink); } .tab.active { color:#0758a8; border-color:#0969da; } .tab:disabled { opacity:.45; cursor:default; }
    .tab-panel { max-width:760px; } .panel-row { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; } .panel-row h2 { margin:0; font-size:15px; } .hint { margin:4px 0 0; color:var(--muted); font-size:12px; } .breadcrumb { display:flex; flex-wrap:wrap; gap:5px; align-items:center; margin:13px 0; color:var(--muted); font-size:12px; } .breadcrumb a { color:#0758a8; } .folder-form { display:flex; gap:8px; margin:0 0 14px; } .folder-form input { min-width:0; flex:1; }
    input,select { width:100%; padding:7px 8px; color:var(--ink); background:#fff; border:1px solid #aeb9c6; border-radius:3px; } input:focus,select:focus { outline:2px solid #b6d6fb; border-color:#0969da; } label { display:block; margin:14px 0 4px; font-size:12px; font-weight:700; } .form-action { margin-top:14px; padding:7px 10px; border:1px solid #0969da; border-radius:3px; color:#fff; background:#0969da; font-weight:700; } .form-action[disabled] { opacity:.6; cursor:wait; }
    .list { border-top:1px solid var(--line); } .item { display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:12px; min-height:38px; padding:7px 0; border-bottom:1px solid var(--line); } .item-main { min-width:0; } .item-main a { overflow-wrap:anywhere; } .item-meta { display:block; color:var(--muted); font-size:11px; } .folder-link { font-weight:700; } .actions { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:8px; font-size:12px; white-space:nowrap; } .actions a { text-decoration:underline; text-underline-offset:2px; }
    .result { margin-top:14px; padding:8px 10px; border-left:3px solid #1a7f37; background:#f1f8f3; font-size:13px; } .result.error { border-color:var(--danger); color:#8a1c12; background:#fff4f2; } .result[hidden] { display:none; } .empty { margin:18px 0; color:var(--muted); }
    @media(max-width:720px) { .topbar { padding:10px 16px; align-items:flex-start; flex-direction:column; } .account { justify-content:flex-start; } #github-login-status { text-align:left; } main { margin:18px auto; } .app-grid { grid-template-columns:1fr; gap:20px; } aside { min-height:0; border-right:0; border-bottom:1px solid var(--line); padding:0 0 18px; } #repository-list { grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); } .tabs { gap:14px; overflow:auto; } .item { grid-template-columns:1fr; gap:4px; } .actions { justify-content:flex-start; } }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand"><svg class="mark" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0a8 8 0 0 0-2.53 15.59c.4.07.55-.17.55-.38l-.01-1.49c-2.01.44-2.43-.85-2.43-.85-.33-.84-.8-1.06-.8-1.06-.55-.38.04-.37.04-.37.61.04.93.63.93.63.54.93 1.42.66 1.77.5.05-.4.21-.66.38-.81-1.61-.18-3.3-.81-3.3-3.59 0-.79.28-1.44.74-1.95-.07-.18-.32-.92.07-1.92 0 0 .6-.19 1.98.74A6.86 6.86 0 0 1 8 1.8c.61 0 1.23.08 1.81.24 1.37-.93 1.98-.74 1.98-.74.39 1 .14 1.74.07 1.92.46.51.74 1.16.74 1.95 0 2.79-1.7 3.4-3.31 3.58.21.18.39.52.39 1.05l-.01 1.55c0 .21.14.46.55.38A8 8 0 0 0 8 0Z"/></svg>GitHub</div>
    <div class="account">
      <span id="account-dot" class="dot{% if not github_account %} off{% endif %}"></span>
      <span id="github-account-label">{% if github_account %}Conectado como <a href="https://github.com/{{ github_account }}" target="_blank" rel="noopener">@{{ github_account }}</a>{% else %}Nenhuma conta conectada{% endif %}</span>
      <button id="github-login" type="button">Conectar/trocar conta</button>
      <button id="github-logout" class="logout" type="button"{% if not github_account %} hidden{% endif %}>Sair</button>
      <span id="github-login-status" hidden></span>
    </div>
  </header>
  <main>
    <div class="app-grid">
      <aside>
        <div class="side-head"><strong>REPOSITÓRIOS</strong><button id="refresh-repositories" class="text-button" type="button">Atualizar</button></div>
        <div id="repository-list"><p class="hint">Conecte uma conta para listar os repositórios.</p></div>
      </aside>
      <section id="workspace" class="workspace" hidden>
        <div class="workspace-head"><strong id="repository-name"></strong><a id="repository-link" target="_blank" rel="noopener">Abrir no GitHub</a></div>
        <nav class="tabs" aria-label="Ações do repositório">
          <button class="tab active" type="button" data-tab="files">ARQUIVOS E PASTAS</button>
          <button class="tab" type="button" data-tab="capture">CAPTURAR PÁGINA</button>
          <button class="tab" type="button" data-tab="upload">UPLOAD DE ARQUIVO</button>
        </nav>
        <section class="tab-panel" data-panel="files">
          <div class="panel-row"><div><h2>Arquivos e pastas</h2><p class="hint">Use uma pasta para organizar as capturas e os envios.</p></div></div>
          <div id="breadcrumb" class="breadcrumb"></div>
          <form id="folder-form" class="folder-form"><input id="new-folder" maxlength="160" placeholder="Nome da nova subpasta" required><button class="form-action" type="submit">Criar pasta</button></form>
          <div id="file-list" class="list"></div>
          <section id="files-result" class="result" hidden></section>
        </section>
        <section class="tab-panel" data-panel="capture" hidden>
          <div class="panel-row"><div><h2>Capturar página</h2><p class="hint">A captura é processada em área temporária e preservada no repositório.</p></div></div>
          <form id="capture-form">
            <input id="capture-repository" name="repository" type="hidden"><input id="capture-branch" name="branch" type="hidden">
            <label for="capture-folder">Pasta de destino</label><select id="capture-folder" name="folder"></select>
            <label for="url">Link da página</label><input id="url" name="url" type="url" placeholder="https://exemplo.com ou https://youtu.be/...?... " required autofocus>
            <p class="hint">Em links do YouTube, o tempo indicado em <code>?t=46m11s</code> ou <code>?t=2771</code> será aplicado antes da captura.</p>
            <label for="label">Nome da captura</label><input id="label" name="label" maxlength="70" placeholder="Ex.: prova-video-audiencia">
            <button id="capture-submit" class="form-action" type="submit">Gerar PDF e guardar no GitHub</button>
          </form>
          <section id="capture-result" class="result" hidden></section>
        </section>
        <section class="tab-panel" data-panel="upload" hidden>
          <div class="panel-row"><div><h2>Upload de arquivo</h2><p class="hint">O original permanece acompanhado de JSON, SHA-256 e registro técnico para download.</p></div></div>
          <form id="upload-form" enctype="multipart/form-data">
            <input id="upload-repository" name="repository" type="hidden"><input id="upload-branch" name="branch" type="hidden">
            <label for="upload-folder">Pasta de destino</label><select id="upload-folder" name="folder"></select>
            <label for="file">Arquivo</label><input id="file" name="file" type="file" required>
            <button id="upload-submit" class="form-action" type="submit">Enviar arquivo ao GitHub</button>
          </form>
          <section id="upload-result" class="result" hidden></section>
        </section>
      </section>
    </div>
  </main>
<script>
(() => {
  const repositoryList = document.querySelector('#repository-list');
  const workspace = document.querySelector('#workspace');
  const repositoryName = document.querySelector('#repository-name');
  const repositoryLink = document.querySelector('#repository-link');
  const fileList = document.querySelector('#file-list');
  const breadcrumb = document.querySelector('#breadcrumb');
  const folderForm = document.querySelector('#folder-form');
  const newFolder = document.querySelector('#new-folder');
  const captureForm = document.querySelector('#capture-form');
  const uploadForm = document.querySelector('#upload-form');
  const captureFolder = document.querySelector('#capture-folder');
  const uploadFolder = document.querySelector('#upload-folder');
  const captureSubmit = document.querySelector('#capture-submit');
  const uploadSubmit = document.querySelector('#upload-submit');
  const githubButton = document.querySelector('#github-login');
  const githubLogout = document.querySelector('#github-logout');
  const githubLabel = document.querySelector('#github-account-label');
  const githubDot = document.querySelector('#account-dot');
  const githubStatus = document.querySelector('#github-login-status');
  const tabs = Array.from(document.querySelectorAll('.tab'));
  const panels = Array.from(document.querySelectorAll('.tab-panel'));
  let repositories = [], selectedRepository = null, currentPath = '', folderPaths = [];

  function api(url, options) { return fetch(url, options || {}).then(async response => { const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Não foi possível concluir a operação.'); return data; }); }
  function showResult(target, message, error) { target.replaceChildren(); target.textContent = message; target.classList.toggle('error', Boolean(error)); target.hidden = false; }
  function resetResult(target) { target.hidden = true; target.classList.remove('error'); target.replaceChildren(); }
  function link(text, href, className) { const item = document.createElement('a'); item.textContent = text; item.href = href; if (className) item.className = className; return item; }
  function query(path) { return '/api/github/documents/download?repository=' + encodeURIComponent(selectedRepository.full_name) + '&path=' + encodeURIComponent(path); }
  function recordPdf(path) { return '/api/github/records/pdf?repository=' + encodeURIComponent(selectedRepository.full_name) + '&path=' + encodeURIComponent(path); }
  function setTab(tabName) { tabs.forEach(tab => tab.classList.toggle('active', tab.dataset.tab === tabName)); panels.forEach(panel => panel.hidden = panel.dataset.panel !== tabName); }
  function addText(target, text, className) { const item = document.createElement('span'); item.textContent = text; if (className) item.className = className; target.append(item); return item; }

  function showGithubAccount(state) {
    githubLabel.replaceChildren();
    if (state.account) {
      githubDot.classList.remove('off'); addText(githubLabel, 'Conectado como ');
      const account = link('@' + state.account, 'https://github.com/' + encodeURIComponent(state.account)); account.target = '_blank'; account.rel = 'noopener'; githubLabel.append(account); githubLogout.hidden = false;
    } else { githubDot.classList.add('off'); githubLabel.textContent = 'Nenhuma conta conectada'; githubLogout.hidden = true; }
  }
  async function refreshGithubLogin() {
    try {
      const state = await api('/api/github/auth'); showGithubAccount(state);
      if (state.running) { githubStatus.hidden = false; githubStatus.textContent = state.device_code ? state.message + ' Código: ' + state.device_code : state.message; window.setTimeout(refreshGithubLogin, 1800); }
      else { githubButton.disabled = false; if (!githubStatus.hidden) { githubStatus.textContent = state.message || ''; window.setTimeout(() => { githubStatus.hidden = true; }, 5000); } if (state.account) loadRepositories(); }
    } catch (_) {}
  }

  function renderRepositories() {
    repositoryList.replaceChildren();
    if (!repositories.length) { const message = document.createElement('p'); message.className = 'hint'; message.textContent = 'Nenhum repositório disponível.'; repositoryList.append(message); return; }
    repositories.forEach(repo => {
      const row = document.createElement('div'); row.className = 'repo';
      const shortName = '/' + repo.full_name.split('/').slice(-1)[0];
      if (selectedRepository && selectedRepository.full_name === repo.full_name) addText(row, shortName, 'active-repo');
      else { const choice = link(shortName, '#'); choice.addEventListener('click', event => { event.preventDefault(); chooseRepository(repo); }); row.append(choice); }
      const visibility = document.createElement('small'); visibility.textContent = repo.private ? 'Privado' : 'Público'; row.append(visibility); repositoryList.append(row);
    });
  }
  async function loadRepositories() {
    repositoryList.replaceChildren(); const loading = document.createElement('p'); loading.className = 'hint'; loading.textContent = 'Carregando…'; repositoryList.append(loading);
    try { const data = await api('/api/github/repositories'); repositories = data.repositories; renderRepositories(); } catch (error) { loading.textContent = error.message; }
  }
  function fillFolderOptions(paths) {
    const values = ['', ...paths];
    [captureFolder, uploadFolder].forEach(select => {
      const previous = select.value;
      select.replaceChildren();
      values.forEach(path => { const option = document.createElement('option'); option.value = path; option.textContent = path ? '/' + path : 'Raiz do repositório'; select.append(option); });
      select.value = values.includes(previous) ? previous : '';
    });
  }
  function renderBreadcrumb(path) {
    breadcrumb.replaceChildren();
    const root = link('Raiz', '#'); root.addEventListener('click', event => { event.preventDefault(); loadContents(''); }); breadcrumb.append(root);
    let built = '';
    path.split('/').filter(Boolean).forEach(part => { addText(breadcrumb, '/'); built = built ? built + '/' + part : part; const itemPath = built; const item = link(part, '#'); item.addEventListener('click', event => { event.preventDefault(); loadContents(itemPath); }); breadcrumb.append(item); });
  }
  function renderFiles(data) {
    fileList.replaceChildren(); renderBreadcrumb(data.path);
    data.folders.forEach(folder => {
      const row = document.createElement('div'); row.className = 'item'; const main = document.createElement('div'); main.className = 'item-main';
      const folderLink = link('▸ ' + folder.name, '#', 'folder-link'); folderLink.addEventListener('click', event => { event.preventDefault(); loadContents(folder.path); }); main.append(folderLink); const meta = document.createElement('span'); meta.className = 'item-meta'; meta.textContent = 'Pasta'; main.append(meta); row.append(main); fileList.append(row);
    });
    data.files.forEach(file => {
      const row = document.createElement('div'); row.className = 'item'; const main = document.createElement('div'); main.className = 'item-main'; const name = link(file.name, query(file.path)); name.setAttribute('download', ''); main.append(name);
      const meta = document.createElement('span'); meta.className = 'item-meta'; meta.textContent = file.kind + ' · ' + file.size + ' bytes'; main.append(meta);
      const actions = document.createElement('div'); actions.className = 'actions';
      actions.append(link('Baixar', query(file.path))); actions.append(link('Registro PDF', recordPdf(file.path)));
      if (file.metadata_path) actions.append(link('JSON', query(file.metadata_path)));
      if (file.integrity_path) actions.append(link('Hash', query(file.integrity_path)));
      const rename = link('Renomear', '#'); rename.addEventListener('click', event => { event.preventDefault(); renameFile(file); }); actions.append(rename);
      row.append(main, actions); fileList.append(row);
    });
    if (!data.folders.length && !data.files.length) { const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = 'Esta pasta está vazia.'; fileList.append(empty); }
  }
  async function loadContents(path) {
    if (!selectedRepository) return;
    fileList.replaceChildren(); const loading = document.createElement('p'); loading.className = 'empty'; loading.textContent = 'Carregando…'; fileList.append(loading);
    try { const data = await api('/api/github/contents?repository=' + encodeURIComponent(selectedRepository.full_name) + '&path=' + encodeURIComponent(path || '')); currentPath = data.path; folderPaths = data.all_folders; fillFolderOptions(folderPaths); renderFiles(data); }
    catch (error) { loading.textContent = error.message; }
  }
  function chooseRepository(repo) {
    selectedRepository = repo; workspace.hidden = false; repositoryName.textContent = repo.full_name; repositoryLink.href = repo.html_url;
    captureForm.querySelector('#capture-repository').value = repo.full_name; captureForm.querySelector('#capture-branch').value = repo.default_branch;
    uploadForm.querySelector('#upload-repository').value = repo.full_name; uploadForm.querySelector('#upload-branch').value = repo.default_branch;
    folderPaths = []; fillFolderOptions(folderPaths); renderRepositories(); setTab('files'); loadContents('');
  }
  async function renameFile(file) {
    const name = window.prompt('Novo nome para "' + file.name + '":', file.name); if (name === null || name.trim() === '' || name === file.name) return;
    try { const data = await api('/api/github/files/rename', { method:'POST', body:new URLSearchParams({repository:selectedRepository.full_name, branch:selectedRepository.default_branch, path:file.path, name:name.trim()}) }); showResult(document.querySelector('#files-result'), data.github_commit_url ? 'Arquivo renomeado no GitHub.' : 'O nome já era o mesmo.'); loadContents(currentPath); }
    catch (error) { showResult(document.querySelector('#files-result'), error.message, true); }
  }
  folderForm.addEventListener('submit', async event => {
    event.preventDefault(); const name = newFolder.value.trim(); if (!name || !selectedRepository) return; const path = currentPath ? currentPath + '/' + name : name; const button = folderForm.querySelector('button'); button.disabled = true;
    try { await api('/api/github/folders', { method:'POST', body:new URLSearchParams({repository:selectedRepository.full_name, branch:selectedRepository.default_branch, folder:path}) }); newFolder.value = ''; showResult(document.querySelector('#files-result'), 'Pasta criada no GitHub.'); await loadContents(currentPath); }
    catch (error) { showResult(document.querySelector('#files-result'), error.message, true); } finally { button.disabled = false; }
  });
  captureForm.addEventListener('submit', async event => {
    event.preventDefault(); if (!selectedRepository) return; resetResult(document.querySelector('#capture-result')); captureSubmit.disabled = true; captureSubmit.textContent = 'Gerando e enviando…';
    try { const data = await api('/api/captures', {method:'POST', body:new FormData(captureForm)}); showResult(document.querySelector('#capture-result'), 'Captura concluída e preservada no GitHub.'); const commit = link(' Ver commit', data.github_commit_url); commit.target = '_blank'; commit.rel = 'noopener'; document.querySelector('#capture-result').append(commit); await loadContents(currentPath); }
    catch (error) { showResult(document.querySelector('#capture-result'), error.message, true); } finally { captureSubmit.disabled = false; captureSubmit.textContent = 'Gerar PDF e guardar no GitHub'; }
  });
  uploadForm.addEventListener('submit', async event => {
    event.preventDefault(); if (!selectedRepository || !uploadForm.querySelector('#file').files[0]) return; resetResult(document.querySelector('#upload-result')); uploadSubmit.disabled = true; uploadSubmit.textContent = 'Enviando…';
    try { const data = await api('/api/github/files', {method:'POST', body:new FormData(uploadForm)}); showResult(document.querySelector('#upload-result'), 'Arquivo preservado no GitHub.'); const commit = link(' Ver commit', data.github_commit_url); commit.target = '_blank'; commit.rel = 'noopener'; document.querySelector('#upload-result').append(commit); uploadForm.querySelector('#file').value = ''; await loadContents(currentPath); }
    catch (error) { showResult(document.querySelector('#upload-result'), error.message, true); } finally { uploadSubmit.disabled = false; uploadSubmit.textContent = 'Enviar arquivo ao GitHub'; }
  });
  tabs.forEach(tab => tab.addEventListener('click', () => { if (selectedRepository) setTab(tab.dataset.tab); }));
  document.querySelector('#refresh-repositories').addEventListener('click', loadRepositories);
  githubButton.addEventListener('click', async () => { githubButton.disabled = true; githubStatus.hidden = false; githubStatus.textContent = 'Iniciando autorização…'; try { await api('/api/github/login', {method:'POST'}); refreshGithubLogin(); } catch (error) { githubStatus.textContent = error.message; githubButton.disabled = false; } });
  githubLogout.addEventListener('click', async () => { if (!window.confirm('Sair da conta do GitHub neste aplicativo?')) return; try { await api('/api/github/logout', {method:'POST'}); showGithubAccount({account:''}); selectedRepository = null; workspace.hidden = true; repositories = []; renderRepositories(); } catch (error) { window.alert(error.message); } });
  if (!githubDot.classList.contains('off')) loadRepositories();
})();
</script>
</body></html>"""


def make_app(output_root: Path = DEFAULT_OUTPUT_DIR) -> Flask:
    application = Flask(__name__)
    output_root = output_root.resolve()
    application.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

    @application.errorhandler(413)
    def upload_too_large(_error):
        return jsonify(error="O arquivo excede o limite de 95 MiB para envio ao GitHub."), 413

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

    @application.post("/api/github/logout")
    def api_github_logout():
        if github_cli_logout():
            return jsonify(account="")
        return jsonify(error="Não foi possível encerrar a sessão do GitHub."), 400

    @application.get("/api/github/repositories")
    def api_github_repositories():
        try:
            return jsonify(repositories=available_github_repositories())
        except GitHubPublishError as error:
            return jsonify(error=str(error)), 400

    @application.get("/api/github/documents")
    def api_github_documents():
        try:
            return jsonify(documents=github_documents(request.args.get("repository", "")))
        except GitHubPublishError as error:
            return jsonify(error=str(error)), 400

    @application.get("/api/github/contents")
    def api_github_contents():
        try:
            return jsonify(
                github_repository_contents(
                    request.args.get("repository", ""),
                    request.args.get("path", ""),
                )
            )
        except GitHubPublishError as error:
            return jsonify(error=str(error)), 400

    @application.get("/api/github/documents/download")
    def api_github_document_download():
        try:
            repository = request.args.get("repository", "")
            path = request.args.get("path", "")
            data = github_document_bytes(repository, path)
            return send_file(
                BytesIO(data),
                mimetype="application/octet-stream",
                as_attachment=True,
                download_name=Path(path).name,
            )
        except GitHubPublishError as error:
            return jsonify(error=str(error)), 400

    @application.get("/api/github/records/pdf")
    def api_github_record_pdf():
        try:
            path = request.args.get("path", "")
            data = github_record_pdf(request.args.get("repository", ""), path)
            return send_file(
                BytesIO(data),
                mimetype="application/pdf",
                as_attachment=True,
                download_name=f"registro_{slugify(Path(path).stem, fallback='arquivo')}.pdf",
            )
        except GitHubPublishError as error:
            return jsonify(error=str(error)), 400

    @application.post("/api/github/folders")
    def api_github_folder_create():
        try:
            commit_url = create_github_folder(
                request.form.get("repository", ""),
                request.form.get("branch", DEFAULT_BRANCH),
                request.form.get("folder", ""),
            )
            return jsonify(github_commit_url=commit_url)
        except GitHubPublishError as error:
            return jsonify(error=str(error)), 400

    @application.post("/api/github/files/rename")
    def api_github_file_rename():
        try:
            commit_url = rename_github_file(
                request.form.get("repository", ""),
                request.form.get("branch", DEFAULT_BRANCH),
                request.form.get("path", ""),
                request.form.get("name", ""),
            )
            return jsonify(github_commit_url=commit_url)
        except GitHubPublishError as error:
            return jsonify(error=str(error)), 400

    @application.post("/api/github/files")
    def api_github_file_upload():
        try:
            repository = github_repository_name(request.form.get("repository", ""))
            uploaded_file = request.files.get("file")
            if uploaded_file is None or not uploaded_file.filename:
                raise GitHubPublishError("Escolha um arquivo para enviar.")
            data = uploaded_file.read(MAX_GITHUB_FILE_BYTES + 1)
            commit_url = publish_uploaded_file(
                repository,
                request.form.get("branch", DEFAULT_BRANCH),
                uploaded_file.filename,
                uploaded_file.mimetype,
                data,
                request.form.get("folder", ""),
            )
            return jsonify(github_commit_url=commit_url)
        except GitHubPublishError as error:
            return jsonify(error=str(error)), 400

    @application.post("/api/captures")
    def api_capture():
        if not CAPTURE_LOCK.acquire(blocking=False):
            return jsonify(error="Já há uma captura em andamento. Aguarde a conclusão."), 409
        try:
            repository = github_repository_name(request.form.get("repository", ""))
            with tempfile.TemporaryDirectory(prefix="pdf-printer-") as temporary_directory:
                result = capture_url(
                    request.form.get("url", ""),
                    Path(temporary_directory),
                    request.form.get("label", ""),
                )
                result.github_commit_url = publish_capture_to_github(
                        result,
                        repository,
                        request.form.get("branch", DEFAULT_BRANCH),
                        folder=request.form.get("folder", ""),
                    )
            return jsonify(
                github_commit_url=result.github_commit_url,
                capture_id=result.capture_id,
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
    parser.add_argument("--repo", help="Repositório do GitHub para publicar, no formato usuario/repositorio.")
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
