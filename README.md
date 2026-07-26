# PDF-printer

Aplicativo local para capturar uma página web em PDF técnico. A saída inclui a página visual integral, uma captura do player de vídeo quando disponível, HTML, metadados e hashes SHA-256.

## Uso local

```powershell
cd "G:\Meu Drive\ESCRITORIO-ADV\APPS-utils\PDF-printer"
python -m pip install -r requirements.txt
python -m playwright install chromium
python PDF_printer.py
```

Abra `http://127.0.0.1:8765` se o navegador não abrir sozinho. Cole a URL e escolha **Gerar captura em PDF**. As saídas ficam em `capturas/<data-hora_nome>/`.

Também é possível automatizar:

```powershell
python PDF_printer.py --url "https://youtu.be/mtRbq5zAZ-E?t=2771" --name "video-exemplo"
```

Em links do YouTube, `t` e `start` são lidos tanto em segundos (`?t=2771`) como em formato humano (`?t=46m11s`). O programa posiciona e pausa o elemento de vídeo antes da imagem do player. A plataforma pode bloquear automações, anúncios, conteúdo restrito ou trechos indisponíveis; nesses casos, o PDF registra a página/viewport que pôde ser exibido.

## Preservação pública no GitHub

Crie primeiro um repositório **público, não vazio** (por exemplo, com um README) e defina a branch `main`. Na interface, marque a opção de publicação, informe `proprietario/repositorio` e um [token fine-grained](https://github.com/settings/personal-access-tokens/new) com a permissão **Contents: Read and write** somente para esse repositório. O token não é salvo.

O aplicativo faz um único commit em `capturas/<id-da-captura>/`, contendo o PDF, HTML, imagens, metadados e `integridade.sha256`. O histórico público do GitHub oferece redundância e registro de versões, mas não equivale a uma ata notarial, assinatura digital qualificada ou carimbo oficial do tempo. Não publique conteúdo sigiloso, dados pessoais sem base legal ou material sem autorização.

## Geração pelo próprio GitHub

O arquivo `.github/workflows/capturar.yml` permite disparar a captura no repositório: **Actions → Gerar captura técnica → Run workflow**. Informe URL, nome e branch. O workflow instala o Chromium, executa o mesmo programa e cria o commit de preservação.

Capturas produzidas no GitHub Actions podem ser bloqueadas por alguns sites — particularmente YouTube, serviços com login, CAPTCHA ou proteção anti-bot. Para esses casos, use a interface local, que opera no seu navegador automatizado local, e publique o resultado pela própria interface.
