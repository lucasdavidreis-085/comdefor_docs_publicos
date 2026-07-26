# PDF-printer

Aplicativo local para capturar uma página web em PDF técnico e preservá-la no repositório GitHub escolhido. A saída inclui a página visual integral, uma captura do player de vídeo quando disponível, HTML, metadados e hashes SHA-256.

## Uso local

```powershell
cd "G:\Meu Drive\ESCRITORIO-ADV\APPS-utils\PDF-printer"
python -m pip install -r requirements.txt
python -m playwright install chromium
python PDF_printer.py
```

Abra `http://127.0.0.1:8765` se o navegador não abrir sozinho. A página lista os repositórios da conta conectada que aceitam gravação. Escolha um deles, cole a URL e gere a captura. A interface usa uma pasta temporária durante o processamento e preserva o resultado no GitHub, sem manter uma cópia automática no computador. Os PDFs já enviados aparecem na própria página com o botão de download.

Também é possível automatizar:

```powershell
python PDF_printer.py --url "https://youtu.be/mtRbq5zAZ-E?t=2771" --name "video-exemplo"
```

Em links do YouTube, `t` e `start` são lidos tanto em segundos (`?t=2771`) como em formato humano (`?t=46m11s`). O programa posiciona e pausa o elemento de vídeo antes da imagem do player. A plataforma pode bloquear automações, anúncios, conteúdo restrito ou trechos indisponíveis; nesses casos, o PDF registra a página/viewport que pôde ser exibido.

## Repositórios e GitHub

Na parte superior da interface, use **Conectar/trocar conta** para autenticar pelo [GitHub CLI](https://cli.github.com/). A página mostra a conta ativa, lista até 100 repositórios públicos ou privados para os quais ela tem permissão de gravação e permite encerrar a sessão com **Sair**. Nenhum token é mostrado ou salvo pelo aplicativo.

O aplicativo faz um único commit em `capturas/<id-da-captura>/`, contendo o PDF, HTML, imagens, metadados e `integridade.sha256`. O histórico do GitHub oferece redundância e registro de versões, mas não equivale a uma ata notarial, assinatura digital qualificada ou carimbo oficial do tempo. Escolha um repositório privado para conteúdo sigiloso e não publique dados pessoais sem base legal ou material sem autorização.

## Geração pelo próprio GitHub

O arquivo `.github/workflows/capturar.yml` permite disparar a captura no repositório: **Actions → Gerar captura técnica → Run workflow**. Informe URL, nome e branch. O workflow instala o Chromium, executa o mesmo programa e cria o commit de preservação.

Capturas produzidas no GitHub Actions podem ser bloqueadas por alguns sites — particularmente YouTube, serviços com login, CAPTCHA ou proteção anti-bot. Para esses casos, use a interface local, que opera no seu navegador automatizado local, e publique o resultado pela própria interface.
