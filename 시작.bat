@echo off
cd /d "%~dp0"
echo 항공권 최저가 검색툴 시작 중...
pip install -r requirements.txt -q
start http://localhost:5060
python app.py
pause
