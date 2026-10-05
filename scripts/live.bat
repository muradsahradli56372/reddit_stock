@echo off
REM One-click LIVE mode on Windows: real data from StockTwits (text) + ApeWisdom (Reddit counts).
REM These settings override anything in .env, so no file editing is needed.
set TEXT_SOURCE=stocktwits
set ATTENTION_SOURCE=apewisdom
set SCHEDULER_ENABLED=true
echo Starting in LIVE mode (StockTwits + ApeWisdom)...
call "%~dp0dev.bat"
