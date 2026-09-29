@echo off
REM Lance le bot MMTV1 en local, DETACHE (survit a la fermeture du terminal / de Claude).
REM Journal : data\local_run.log (ajout). Dashboard : http://127.0.0.1:8787
cd /d "%~dp0"
venv\Scripts\python.exe -u _local_launcher.py >> data\local_run.log 2>&1
