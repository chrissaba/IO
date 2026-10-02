@echo off
rem Starts the boss model (Gemma 4 E4B, with vision for UFO2) on http://127.0.0.1:8090 using Unsloth Studio's llama.cpp build.
rem UI-TARS-1.5-7B (the "eyes") must be loaded in Unsloth Studio, which serves it on port 8888.
rem Studio's llama-server needs the CUDA libraries that ship inside Studio's torch install.
set "PATH=%USERPROFILE%\.unsloth\studio\unsloth_studio\Lib\site-packages\torch\lib;%PATH%"
set "SNAP=%USERPROFILE%\.cache\huggingface\hub\models--unsloth--gemma-4-E4B-it-GGUF\snapshots\bfc15c382204943c3a8fff0c750b94ae2364d7a3"
"%USERPROFILE%\.unsloth\llama.cpp\build\bin\Release\llama-server.exe" -m "%SNAP%\gemma-4-E4B-it-Q4_K_M.gguf" --mmproj "%SNAP%\mmproj-F16.gguf" --host 127.0.0.1 --port 8090 -c 32768 -ngl 99 --jinja --parallel 1 --flash-attn on --reasoning off --alias boss
