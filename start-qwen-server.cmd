@echo off
rem "Smart" mode: Qwen 3.8 27B on http://127.0.0.1:8090 as both the boss and the eyes (its own vision), with a short
rem thinking budget. Uses Unsloth Studio's llama.cpp build, which needs the CUDA libraries inside Studio's torch install.
set "PATH=%USERPROFILE%\.unsloth\studio\unsloth_studio\Lib\site-packages\torch\lib;%PATH%"
set "SNAP=%USERPROFILE%\.cache\huggingface\hub\models--unsloth--Qwen3.8-27B-GGUF\snapshots\4ca720788d1e01f1bff70c033e0d0028fd02e502"
"%USERPROFILE%\.unsloth\llama.cpp\build\bin\Release\llama-server.exe" -m "%SNAP%\Qwen3.8-27B-UD-Q4_K_M.gguf" --mmproj "%SNAP%\mmproj-F16.gguf" --host 127.0.0.1 --port 8090 -c 32768 -ngl 99 --jinja --parallel 1 --flash-attn on --reasoning on --reasoning-budget 256 --alias boss
