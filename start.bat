@echo off
title cansim
cd /d "%~dp0"
echo.
echo   ============================================
echo     cansim - symulator magistrali CAN (STM32)
echo   ============================================
echo.

rem --- Python: najpierw launcher "py" (python.org), potem "python" ---
set PY=
where py >nul 2>nul && set PY=py
if not defined PY (
  where python >nul 2>nul && python -c "import sys" >nul 2>nul && set PY=python
)
if not defined PY (
  echo   [BLAD] Nie znaleziono Pythona.
  echo          Zainstaluj go z https://www.python.org/downloads/
  echo          i zaznacz w instalatorze "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

rem --- gcc ---
where gcc >nul 2>nul
if errorlevel 1 (
  echo   [UWAGA] Nie znaleziono kompilatora gcc w PATH.
  echo           Plytki sie nie skompiluja. Instrukcja: README.md, sekcja Windows.
  echo.
)

rem --- naglowki ST ---
if not exist "third_party\cmsis_device_h7\Include" (
  echo   [UWAGA] Brak naglowkow ST w folderze third_party.
  choice /c TN /m "          Pobrac je teraz (ok. 70 MB)"
  if not errorlevel 2 call "%~dp0pobierz_naglowki_st.bat" nopause
  echo.
)

echo   Uruchamiam... przegladarka otworzy sie sama.
echo   Nie zamykaj tego okna, dopoki uzywasz programu (Ctrl+C konczy).
echo.
%PY% -m cansim %*
echo.
pause
