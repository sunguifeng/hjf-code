@echo off
rem %~dp0 = 本脚本所在目录（自带尾部反斜杠）：项目挪窝不用改这里
cd /d "%~dp0"
.venv\Scripts\python.exe -m mychat %*
