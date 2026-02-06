# vorax-news-digest

Vorax News Digest (VLR + THESPIKE) — Setup direto

Arquivos:
- bot_noticias_digest_ptbr.py   (bot)
- run_digest.ps1                (runner para agendador)
- webhook_url.txt               (URL do webhook — 1 linha, segredo)

1) Instalar dependências (na venv):
   cd C:\vorax-noticias
   .\.venv\Scripts\Activate.ps1
   pip install requests beautifulsoup4 lxml

2) Webhook (recomendado):
   - Crie C:\vorax-noticias\webhook_url.txt com a URL (uma linha)
   - NÃO compartilhe / não suba no Git.

3) Rodar manual:
   python .\bot_noticias_digest_ptbr.py

4) Rodar via runner (gera log):
   powershell -ExecutionPolicy Bypass -File .\run_digest.ps1
   type .\runs.log

5) Agendador de Tarefas (08:00 e 18:00):
   - Criar Tarefa...
   - Triggers: Daily 08:00 e Daily 18:00
   - Action:
       Program: powershell.exe
       Args: -NoProfile -ExecutionPolicy Bypass -File "C:\vorax-noticias\run_digest.ps1"
       Start in: C:\vorax-noticias
   - Teste: botão direito na tarefa -> Executar
   - Sucesso: Last Run Result = 0x0 + runs.log atualizado
