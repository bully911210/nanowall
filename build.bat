@echo off
rem Rebuilds nanowall.exe into Desktop\nanowall-app. Double-click or run from anywhere.
cd /d "%~dp0"
python -m PyInstaller nanowall.spec --noconfirm || (echo BUILD FAILED & pause & exit /b 1)
if not exist "%USERPROFILE%\Desktop\nanowall-app" mkdir "%USERPROFILE%\Desktop\nanowall-app"
copy /y dist\nanowall.exe "%USERPROFILE%\Desktop\nanowall-app\nanowall.exe" || (echo Close nanowall.exe first, then retry. & pause & exit /b 1)
rmdir /s /q build dist
echo Done: %USERPROFILE%\Desktop\nanowall-app\nanowall.exe
pause
