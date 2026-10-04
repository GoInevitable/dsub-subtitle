@echo off
rem dsub launcher - add this folder to PATH, then run "dsub" anywhere.
setlocal
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% "%~dp0dsub.py" %*
exit /b %errorlevel%
