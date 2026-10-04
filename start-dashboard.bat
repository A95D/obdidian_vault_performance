@echo off
cd /d "%~dp0"
set PORT=8787

netstat -ano | findstr ":%PORT% " | findstr LISTENING >nul
if %errorlevel% neq 0 (
    start "" pythonw -c "import http.server,socketserver;H=type('H',(http.server.SimpleHTTPRequestHandler,),{'protocol_version':'HTTP/1.1','log_message':lambda *a:None});socketserver.ThreadingTCPServer.allow_reuse_address=True;socketserver.ThreadingTCPServer(('',8787),H).serve_forever()"
    timeout /t 2 /nobreak >nul
)

start "" http://localhost:%PORT%/dashboard.html
exit