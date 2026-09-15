param(
    [Parameter(Mandatory = $true)]
    [string]$Root
)

$ErrorActionPreference = "Stop"
$rootPath = [IO.Path]::GetFullPath($Root.Trim().Trim('"')).TrimEnd('\')
$revision = "97429c5623590d52f001249460daf43e6749d777"
$repository = Join-Path $rootPath "downloads\engines\windows-driver-samples"
$sample = Join-Path $repository "audio\simpleaudiosample"
$patch = Join-Path $rootPath "backend\engines\virtual_audio\microsoft-simpleaudiosample.patch"
$output = Join-Path $rootPath "generated\build\virtual-audio"
$hardwareId = "ROOT\ADVoiceVirtualAudio"
$wdkVersion = "10.0.26100.6584"
$wdkBuildFolder = "10.0.26100.0"

function Install-NuGetArchive([string]$Id, [string]$Version, [string]$Destination) {
    $marker = Join-Path $Destination ".advoice-extracted"
    if (Test-Path -LiteralPath $marker -PathType Leaf) { return }
    $packages = Join-Path $rootPath "downloads\packages"
    New-Item -ItemType Directory -Force -Path $packages | Out-Null
    $lowerId = $Id.ToLowerInvariant()
    $archive = Join-Path $packages "$lowerId.$Version.nupkg"
    $zip = Join-Path $packages "$lowerId.$Version.zip"
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        $url = "https://api.nuget.org/v3-flatcontainer/$lowerId/$Version/$lowerId.$Version.nupkg"
        Invoke-WebRequest -Uri $url -OutFile $archive
    }
    Copy-Item -LiteralPath $archive -Destination $zip -Force
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Expand-Archive -LiteralPath $zip -DestinationPath $Destination -Force
    [IO.File]::WriteAllText($marker, "$Id $Version", [Text.UTF8Encoding]::new($false))
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required to restore the pinned Microsoft virtual audio driver source"
}
if (-not (Test-Path -LiteralPath (Join-Path $repository ".git") -PathType Container)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $repository) | Out-Null
    & git clone --filter=blob:none --no-checkout https://github.com/microsoft/Windows-driver-samples.git $repository
    if ($LASTEXITCODE -ne 0) { throw "Could not download the Microsoft driver sample" }
}

& git -C $repository sparse-checkout init --cone
& git -C $repository sparse-checkout set audio/simpleaudiosample
$current = (& git -C $repository rev-parse HEAD 2>$null).Trim()
if ($current -ne $revision) {
    & git -C $repository fetch --depth 1 origin $revision
    if ($LASTEXITCODE -ne 0) { throw "Could not restore pinned Microsoft driver revision $revision" }
    & git -C $repository checkout --detach $revision
    if ($LASTEXITCODE -ne 0) { throw "Could not select pinned Microsoft driver revision $revision" }
}

$savedErrorPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& git -C $repository apply --reverse --check --ignore-space-change $patch 2>$null
$alreadyPatched = $LASTEXITCODE -eq 0
$ErrorActionPreference = $savedErrorPreference
if (-not $alreadyPatched) {
    & git -C $repository apply --check --ignore-space-change $patch
    if ($LASTEXITCODE -ne 0) { throw "The A&D Voice virtual audio patch does not match the pinned Microsoft source" }
    & git -C $repository apply $patch
    if ($LASTEXITCODE -ne 0) { throw "Could not apply the A&D Voice virtual audio patch" }
}

# Microsoft's sample INF is UTF-16 LE. Brand it without silently changing its
# encoding, because Inf2Cat/StampInf consume the exact generated file later.
$inx = Join-Path $sample "Source\Main\SimpleAudioSample.inx"
$encoding = [Text.Encoding]::Unicode
$text = [IO.File]::ReadAllText($inx, $encoding)
$replacements = [ordered]@{
    "ROOT\SimpleAudioSample" = $hardwareId
    "Virtual Audio Device (WDM) - Simple Audio Sample" = "A&D Voice Virtual Audio Bridge"
    "Virtual Audio Device (WDM) - Simple Audio Sample Driver" = "A&D Voice Virtual Audio Bridge Driver"
    "Simple Audio Sample Wave Speaker" = "A&D Voice Virtual Microphone Feed"
    "Simple Audio Sample Topology Speaker" = "A&D Voice Virtual Microphone Feed"
    "Simple Audio Sample Wave Microphone Array - Front" = "A&D Voice Virtual Microphone"
    "Simple Audio Sample Topology Microphone Array - Front" = "A&D Voice Virtual Microphone"
    "Internal Microphone Array - Front" = "A&D Voice Virtual Microphone"
}
foreach ($entry in $replacements.GetEnumerator()) { $text = $text.Replace($entry.Key, $entry.Value) }
[IO.File]::WriteAllText($inx, $text, $encoding)

$packageRoot = Join-Path $repository "packages"
$wdkPackage = Join-Path $packageRoot "Microsoft.Windows.WDK.x64.$wdkVersion"
$sdkPackage = Join-Path $packageRoot "Microsoft.Windows.SDK.CPP.$wdkVersion"
$sdkX64Package = Join-Path $packageRoot "Microsoft.Windows.SDK.CPP.x64.$wdkVersion"
Install-NuGetArchive "Microsoft.Windows.WDK.x64" $wdkVersion $wdkPackage
Install-NuGetArchive "Microsoft.Windows.SDK.CPP" $wdkVersion $sdkPackage
Install-NuGetArchive "Microsoft.Windows.SDK.CPP.x64" $wdkVersion $sdkX64Package

$buildProps = @"
<Project>
  <Import Project="packages\Microsoft.Windows.SDK.CPP.x64.$wdkVersion\build\native\Microsoft.Windows.SDK.cpp.x64.props" />
  <Import Project="packages\Microsoft.Windows.SDK.CPP.$wdkVersion\build\native\Microsoft.Windows.SDK.cpp.props" />
  <Import Project="packages\Microsoft.Windows.WDK.x64.$wdkVersion\build\native\Microsoft.Windows.WDK.x64.props" />
</Project>
"@
[IO.File]::WriteAllText((Join-Path $repository "Directory.Build.props"), $buildProps, [Text.UTF8Encoding]::new($false))
Copy-Item -LiteralPath (Join-Path $rootPath "backend\engines\virtual_audio\toolset\WindowsDriver.OS.props") `
    -Destination (Join-Path $wdkPackage "c\build\$wdkBuildFolder\WindowsDriver.OS.props") -Force

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$visualStudio = ""
if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
    $visualStudio = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
}
if ($visualStudio) { $visualStudio = $visualStudio.Trim() }
if (-not $visualStudio) {
    $visualStudio = @(
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2022\BuildTools"),
        (Join-Path $env:ProgramFiles "Microsoft Visual Studio\2022\Community"),
        (Join-Path $env:ProgramFiles "Microsoft Visual Studio\2022\Professional"),
        (Join-Path $env:ProgramFiles "Microsoft Visual Studio\2022\Enterprise")
    ) | Where-Object { Test-Path -LiteralPath (Join-Path $_ "MSBuild\Current\Bin\MSBuild.exe") } | Select-Object -First 1
}
if (-not $visualStudio) { throw "Visual Studio 2022 C++ build tools are missing" }
$msbuild = Join-Path $visualStudio "MSBuild\Current\Bin\MSBuild.exe"
$vctargetSource = Join-Path $visualStudio "MSBuild\Microsoft\VC\v170"
$vctargets = Join-Path $output "vctargets"
New-Item -ItemType Directory -Force -Path $vctargets | Out-Null
Copy-Item -Path (Join-Path $vctargetSource "*") -Destination $vctargets -Recurse -Force
$portableToolset = Join-Path $vctargets "Platforms\x64\PlatformToolsets\WindowsKernelModeDriver10.0"
New-Item -ItemType Directory -Force -Path $portableToolset | Out-Null
Copy-Item -Path (Join-Path $rootPath "backend\engines\virtual_audio\toolset\Toolset.*") -Destination $portableToolset -Force

$mainProject = Join-Path $sample "Source\Main\Main.vcxproj"
& $msbuild $mainProject /t:Rebuild /p:Configuration=Release /p:Platform=x64 "/p:VCTargetsPath=$vctargets\" /m
if ($LASTEXITCODE -ne 0) { throw "A&D Voice virtual audio driver compilation failed" }

$driver = Join-Path $sample "Source\Main\x64\Release\SimpleAudioSample.sys"
if (-not (Test-Path -LiteralPath $driver -PathType Leaf)) { throw "The built virtual audio driver was not found" }
$driverPackage = Join-Path $output "package"
New-Item -ItemType Directory -Force -Path $driverPackage | Out-Null
Copy-Item -LiteralPath $driver -Destination $driverPackage -Force
Copy-Item -LiteralPath (Join-Path $rootPath "backend\engines\virtual_audio\LICENSE-MS-PL.txt") `
    -Destination $driverPackage -Force
$infText = [IO.File]::ReadAllText($inx, $encoding).Replace('$ARCH$', 'amd64').Replace('$KMDFVERSION$', '1.15')
$inf = Join-Path $driverPackage "ADVoiceVirtualAudio.inf"
[IO.File]::WriteAllText($inf, $infText, [Text.Encoding]::Unicode)
$inf2cat = Join-Path $wdkPackage "c\bin\$wdkBuildFolder\x86\Inf2Cat.exe"
if (-not (Test-Path -LiteralPath $inf2cat -PathType Leaf)) { throw "Inf2Cat was not restored with the WDK package" }
& $inf2cat "/driver:$driverPackage" /os:10_X64
if ($LASTEXITCODE -ne 0) { throw "The A&D Voice virtual audio driver package is not signable" }
Write-Host "A&D Voice virtual audio package is ready: $output"
