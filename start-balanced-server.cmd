@echo off
rem IO's local models, side by side on the 24 GB card (every effort level uses these two):
rem   EvoCUA-8B (Meituan's computer-use model, 4-bit + its vision part) sees and clicks, on http://127.0.0.1:8091
rem   Muse Glimmer 30B (3-bit, loaded without its vision part) thinks and calls tools, on http://127.0.0.1:8090
rem EvoCUA loads first so the larger model fits around it. Both are in %USERPROFILE%\models.
rem Reasoning is set per request (boss.EFFORT): --reasoning-budget does nothing for Glimmer, so none is set here.
set "PATH=%USERPROFILE%\.unsloth\studio\unsloth_studio\Lib\site-packages\torch\lib;%PATH%"
set "LLAMA=%USERPROFILE%\.unsloth\llama.cpp\build\bin\Release\llama-server.exe"
set "M=%USERPROFILE%\models"
start "" /b "%LLAMA%" -m "%M%\evocua-8b-UD-Q4_K_XL.gguf" --mmproj "%M%\mmproj-evocua-8b-f16.gguf" --host 127.0.0.1 --port 8091 -c 8192 -ngl 99 --jinja --parallel 1 --flash-attn on -ctk q8_0 -ctv q8_0 --alias eyes
rem wait for EvoCUA to finish loading (up to 3 minutes); ping, since timeout needs a console and IO starts this without one
set /a WAITED=0
:wait_eyes
ping -n 3 127.0.0.1 >nul
set /a WAITED+=1
if %WAITED% gtr 90 goto load_boss
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8091/health | findstr 200 >nul || goto wait_eyes
:load_boss
"%LLAMA%" -m "%M%\Muse-Glimmer-30B-UD-Q3_K_XL.gguf" --host 127.0.0.1 --port 8090 -c 32768 -ngl 99 --jinja --parallel 1 --flash-attn on -ctk q8_0 -ctv q8_0 --reasoning on --alias boss
