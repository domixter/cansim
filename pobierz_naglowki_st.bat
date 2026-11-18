@echo off
rem cansim: pobiera oficjalne naglowki ST (CMSIS device H7 i F4) do third_party\
rem Nie wymaga gita ani Pythona (uzywa PowerShell wbudowanego w Windows). Potrzebny internet.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\pobierz_naglowki_st.ps1"
if /i not "%~1"=="nopause" pause
