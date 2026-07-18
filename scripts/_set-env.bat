@echo off
reg add "HKLM\SYSTEM\CurrentControlSet\Services\hermes-orch-agent" /v Environment /t REG_MULTI_SZ /d "HERMES_BIN=C:\Users\stanley\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe\0HERMES_HOME=C:\Users\stanley\AppData\Local\hermes" /f
echo Exit: %ERRORLEVEL%
reg query "HKLM\SYSTEM\CurrentControlSet\Services\hermes-orch-agent" /v Environment
