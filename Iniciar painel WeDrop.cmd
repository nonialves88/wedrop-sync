@echo off
rem Inicia o painel local do sincronizador WeDrop e abre no navegador.
rem Uso: duplo clique neste arquivo.
setlocal
cd /d "%~dp0api-sync"

set "PORTA=8790"
set "URL=http://127.0.0.1:%PORTA%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "if (Test-NetConnection -ComputerName 127.0.0.1 -Port %PORTA% -InformationLevel Quiet -WarningAction SilentlyContinue) { exit 0 } else { exit 1 }"

if %ERRORLEVEL%==0 (
  echo Painel ja esta rodando. Abrindo %URL% ...
  start "" "%URL%"
  exit /b 0
)

echo Subindo o painel em %URL% ...
start "WeDrop sync" /min python -m wedrop_sync serve --home state --port %PORTA% --open
exit /b 0
