@echo off
rem Install or update PyStarLight (Windows): git pull, system check, uv sync, check.
rem   install.cmd                    update, with the extras chosen before (first time: all camera)
rem   install.cmd all camera         choose the extras (remembered)
rem   install.cmd --help             all options (scripts\install.py)
rem From PowerShell: .\install.cmd
setlocal
cd /d "%~dp0"
where uv >nul 2>nul || if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul || if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
where uv >nul 2>nul || (echo uv not found. Install it, open a new terminal and run install.cmd again: & echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" & exit /b 1)
set VIRTUAL_ENV=
rem one line: cmd reads this file while it runs, and git pull may rewrite it meanwhile
uv run --no-project scripts\install.py %* & exit /b
