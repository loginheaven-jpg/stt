@echo off
rem ============================================================
rem  run_all.cmd — 5개 엔진 일괄 전사와 채점
rem
rem  사용법
rem    run_all.cmd -p
rem    run_all.cmd sample3.wav sample3.txt 2 이승은,황미혜,이대표님
rem    run_all.cmd sample.mp3  ref_A.txt   0 김도현,변규리,류지현
rem
rem  인자 순서 : 음원  정답지  화자수  키워드(쉼표구분)
rem  정답지를 비우려면 "" 를 넣는다.
rem
rem  .ps1 과 달리 실행 정책의 영향을 받지 않는다.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul

set PY=python
set ENGINES=rtzr_sommers,rtzr_whisper,clova,openai,whisper_local

if "%~1"=="" goto usage
if /I "%~1"=="-p"          goto preflight
if /I "%~1"=="--preflight" goto preflight
if /I "%~1"=="-h"          goto usage
if /I "%~1"=="--help"      goto usage

set "AUDIO=%~1"
set "REF=%~2"
set "SPK=%~3"
set "KW=%~4"
if "%SPK%"=="" set SPK=0

if not exist "%AUDIO%" (
    echo.
    echo   음원을 찾지 못했다: %AUDIO%
    goto end
)

for %%F in ("%AUDIO%") do set "STEM=%%~nF"
set "OUTDIR=out_%STEM%"

echo.
echo ==============================================
echo   1/3   사전 점검
echo ==============================================
%PY% run_stt.py --preflight --engines %ENGINES% --speakers %SPK%
if errorlevel 1 (
    echo.
    echo   준비되지 않은 엔진이 있다.
    echo   그대로 진행하려면 아무 키나, 중단하려면 Ctrl+C.
    pause >nul
)

echo.
echo ==============================================
echo   2/3   전사   %AUDIO%  화자 %SPK%  ^-^> %OUTDIR%
echo ==============================================
if "%KW%"=="" (
    %PY% run_stt.py "%AUDIO%" --speakers %SPK% --engines %ENGINES% --outdir "%OUTDIR%"
) else (
    %PY% run_stt.py "%AUDIO%" --speakers %SPK% --engines %ENGINES% --outdir "%OUTDIR%" --keywords %KW:,= %
)

if "%REF%"=="" (
    echo.
    echo   정답지가 없어 채점은 건너뛴다. 결과는 %OUTDIR% 에 있다.
    goto end
)
if not exist "%REF%" (
    echo.
    echo   정답지를 찾지 못했다: %REF%
    goto end
)

echo.
echo ==============================================
echo   3/3   채점   %REF%
echo ==============================================
if "%KW%"=="" (
    %PY% stt_bench.py "%REF%" "%OUTDIR%\%STEM%__*.txt" --detail
) else (
    %PY% stt_bench.py "%REF%" "%OUTDIR%\%STEM%__*.txt" --detail --nouns %KW:,= %
)
goto end

:preflight
%PY% run_stt.py --preflight --engines %ENGINES%
goto end

:usage
echo.
echo   run_all.cmd -p
echo   run_all.cmd ^<음원^> ^<정답지^> ^<화자수^> ^<키워드,쉼표,구분^>
echo.
echo   예)  run_all.cmd sample3.wav sample3.txt 2 이승은,황미혜,이대표님
echo        run_all.cmd sample.mp3 "" 0
echo.

:end
endlocal
