@echo off
rem "Balanced" mode: Qwen 3.6 35B-A3B (mixture of experts, about 3B active per token) on http://127.0.0.1:8090 as both the
rem boss and the eyes, with a short thinking budget. A quantized KV cache keeps the 21 GB model inside a 24 GB GPU.
set "PATH=%USERPROFILE%\.unsloth\studio\unsloth_studio\Lib\site-packages\torch\lib;%PATH%"
for /d %%D in ("%USERPROFILE%\.cache\huggingface\hub\models--unsloth--Qwen3.6-35B-A3B-GGUF\snapshots\*") do set "SNAP=%%D"
"%USERPROFILE%\.unsloth\llama.cpp\build\bin\Release\llama-server.exe" -m "%SNAP%\Qwen3.6-35B-A3B-UD-Q4_K_S.gguf" --mmproj "%SNAP%\mmproj-F16.gguf" --host 127.0.0.1 --port 8090 -c 16384 -ngl 99 --jinja --parallel 1 --flash-attn on -ctk q8_0 -ctv q8_0 --reasoning on --reasoning-budget 256 --alias boss
