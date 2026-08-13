# run_all.ps1 — 5개 엔진 일괄 전사와 채점
#
# 사용법
#   .\run_all.ps1 -Preflight                            사전 점검만
#   .\run_all.ps1 -Audio sample3.wav -Ref sample3.txt -Speakers 2 -Keywords 이승은,황미혜,이대표님
#   .\run_all.ps1 -Audio sample.mp3  -Ref ref_A.txt     -Speakers 0 -Keywords 김도현,변규리
#
# 여러 줄 붙여넣기로 명령이 끊기는 사고를 막기 위한 스크립트다.

param(
    [string]   $Audio    = "",
    [string]   $Ref      = "",
    [int]      $Speakers = 0,
    [string]   $Keywords = "",
    [string]   $Engines  = "rtzr_sommers,rtzr_whisper,clova,openai,whisper_local",
    [string]   $OutDir   = "",
    [switch]   $Preflight
)

$ErrorActionPreference = "Stop"
$py = "python"

# 쉼표로 받은 키워드를 인자 배열로 편다
$kw = @()
if ($Keywords) { $kw = $Keywords.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ } }

if ($Preflight) {
    & $py run_stt.py --preflight --engines $Engines --speakers $Speakers
    exit $LASTEXITCODE
}

if (-not $Audio) { Write-Host "-Audio 를 지정해달라." -ForegroundColor Yellow; exit 1 }
if (-not (Test-Path $Audio)) { Write-Host "음원을 찾지 못했다: $Audio" -ForegroundColor Yellow; exit 1 }

# 출력 폴더를 지정하지 않으면 음원 이름으로 만든다
if (-not $OutDir) { $OutDir = "out_" + [System.IO.Path]::GetFileNameWithoutExtension($Audio) }

Write-Host ""
Write-Host "■ 1/3  사전 점검" -ForegroundColor Cyan
& $py run_stt.py --preflight --engines $Engines --speakers $Speakers
if ($LASTEXITCODE -ne 0) {
    Write-Host "준비되지 않은 엔진이 있다. 그대로 진행하려면 아무 키나, 중단하려면 Ctrl+C." -ForegroundColor Yellow
    [void][System.Console]::ReadKey($true)
}

Write-Host ""
Write-Host "■ 2/3  전사  ($Audio · 화자 $Speakers · $OutDir)" -ForegroundColor Cyan
$args2 = @($Audio, "--speakers", $Speakers, "--engines", $Engines, "--outdir", $OutDir)
if ($kw.Count) { $args2 += "--keywords"; $args2 += $kw }
& $py run_stt.py @args2

if (-not $Ref) {
    Write-Host ""
    Write-Host "정답지(-Ref)가 없어 채점은 건너뛴다. 결과는 $OutDir 에 있다." -ForegroundColor Yellow
    exit 0
}
if (-not (Test-Path $Ref)) { Write-Host "정답지를 찾지 못했다: $Ref" -ForegroundColor Yellow; exit 1 }

Write-Host ""
Write-Host "■ 3/3  채점  ($Ref)" -ForegroundColor Cyan
$stem = [System.IO.Path]::GetFileNameWithoutExtension($Audio)
$args3 = @($Ref, (Join-Path $OutDir "$stem`__*.txt"), "--detail")
if ($kw.Count) { $args3 += "--nouns"; $args3 += $kw }
& $py stt_bench.py @args3
