param(
    [ValidateSet("full","fast","installer","setup","clean")]
    [string]$Mode = "full",

    [ValidateSet("","backend","asio","frontend","models")]
    [string]$Worker = "",

    [switch]$SkipReleaseGate,
    [switch]$SkipPackageSmoke
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "front"
$Build = Join-Path $Root "generated\build"
$Downloads = Join-Path $Root "downloads"
$Release = Join-Path $Root "release"

$BackendDist = Join-Path $Build "backend\dist\KaraokeBackend"
$TempDir = Join-Path $Build "installer-temp"
$StateDir = Join-Path $Build ".state"
$TimingDir = Join-Path $StateDir "timings"

$ElectronRoot = Join-Path $Build "electron"
$ElectronCurrentFile = Join-Path $StateDir "electron-current.txt"
$Unpacked = Join-Path $ElectronRoot "win-unpacked"

$InstallerRoot = Join-Path $Build "installer"
$InstallerCurrentFile = Join-Path $StateDir "installer-current.txt"
$InstallerDir = Join-Path $InstallerRoot "current"

if (Test-Path -LiteralPath $ElectronCurrentFile -PathType Leaf) {
    $savedElectronPath = (
        Get-Content -LiteralPath $ElectronCurrentFile -ErrorAction SilentlyContinue |
        Select-Object -First 1
    )

    if ($savedElectronPath -and
        (Test-Path -LiteralPath $savedElectronPath -PathType Container)) {
        $Unpacked = [IO.Path]::GetFullPath($savedElectronPath)
    }
}

if (Test-Path -LiteralPath $InstallerCurrentFile -PathType Leaf) {
    $savedInstallerPath = (
        Get-Content -LiteralPath $InstallerCurrentFile -ErrorAction SilentlyContinue |
        Select-Object -First 1
    )

    if ($savedInstallerPath -and
        (Test-Path -LiteralPath $savedInstallerPath -PathType Container)) {
        $InstallerDir = [IO.Path]::GetFullPath($savedInstallerPath)
    }
}
$ParallelDir = Join-Path $Build "parallel"

$Python = Join-Path $Backend "venv\Scripts\python.exe"
$PackagedBackend = Join-Path $Unpacked "resources\backend"

$SceneVideoSource = Join-Path $Downloads "media\videoplayback.webm"
$PackagedSceneVideo = Join-Path $Unpacked "resources\media\videoplayback.webm"

$Asio = Join-Path $Backend "engines\asio"
$AsioBuild = Join-Path $Build "asio"
$AsioSdk = Join-Path $Downloads "engines\asio-sdk"
$VirtualAudioPrepare = Join-Path $Root "scripts\prepare-virtual-audio.ps1"
$VirtualAudioBuild = Join-Path $Build "virtual-audio"
$VirtualAudioPackage = Join-Path $VirtualAudioBuild "package"

$Models = Join-Path $Downloads "models"
$MsstEngine = Join-Path $Downloads "engines\msst"

function Find-VisualStudioInstallation {
    $Override = $env:ADVOICE_VS_PATH
    if ($Override) {
        $ResolvedOverride = [IO.Path]::GetFullPath($Override)
        if (-not (Test-Path -LiteralPath $ResolvedOverride -PathType Container)) {
            throw "ADVOICE_VS_PATH does not point to a Visual Studio installation: $ResolvedOverride"
        }
        return $ResolvedOverride
    }

    $VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $VsWhere -PathType Leaf)) {
        throw "Visual Studio locator was not found. Install Visual Studio 2022 Build Tools or Community with 'Desktop development with C++' and CMake, or set ADVOICE_VS_PATH."
    }
    $Candidates = @(& $VsWhere -latest -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -requires Microsoft.VisualStudio.Component.VC.CMake.Project `
        -property installationPath)
    $VsWhereExitCode = $LASTEXITCODE
    $Installation = $Candidates | Where-Object { $_ -and $_.Trim() } | Select-Object -First 1
    if ($VsWhereExitCode -ne 0 -or -not $Installation) {
        throw "No compatible Visual Studio installation was found. Required components: Microsoft.VisualStudio.Component.VC.Tools.x86.x64 and Microsoft.VisualStudio.Component.VC.CMake.Project."
    }
    return [IO.Path]::GetFullPath($Installation.Trim())
}

$Vs = Find-VisualStudioInstallation
$VcVars = Join-Path $Vs "VC\Auxiliary\Build\vcvars64.bat"
$CMake = Join-Path $Vs "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
$Ninja = Join-Path $Vs "Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"

$AppName = "A&D Voice"

# Validate the exact checked-out source before a clean build changes any
# version-bearing manifest. A failed gate must leave the working tree byte-for-byte
# unchanged so retrying the same release input cannot accidentally bump twice.
if (-not $Worker) {
    if ($SkipReleaseGate) {
        Write-Warning "Release gate skipped by -SkipReleaseGate. Tests, mutation checks and browser E2E are NOT being run."
    }
    else {
        Write-Host "Running mandatory release gate..."
        $ReleaseGate = Join-Path $Root "verify-release.bat"
        if (-not (Test-Path -LiteralPath $ReleaseGate -PathType Leaf)) {
            throw "Release gate was not found: $ReleaseGate"
        }
        $PreviousReleaseFull = $env:KARAOKE_RELEASE_FULL
        try {
            if ($Mode -eq "clean") { $env:KARAOKE_RELEASE_FULL = "1" }
            & $ReleaseGate
            if ($LASTEXITCODE -ne 0) {
                throw "Release gate failed. Installer build is blocked."
            }
        }
        finally {
            if ($null -eq $PreviousReleaseFull) {
                Remove-Item Env:KARAOKE_RELEASE_FULL -ErrorAction SilentlyContinue
            }
            else {
                $env:KARAOKE_RELEASE_FULL = $PreviousReleaseFull
            }
        }
    }
}

# Repeated full/fast builds keep the current application version so unchanged
# components and the installer can be reused. A clean release build explicitly
# advances the patch version and rebuilds every version-bearing artifact.
# Worker sub-processes must never bump independently.
if (-not $Worker -and $Mode -eq "clean") {
    function Get-NextPatchVersion([string]$Version) {
        if ($Version -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
            throw "Version '$Version' is not in major.minor.patch form"
        }
        return "{0}.{1}.{2}" -f $Matches[1], $Matches[2], ([int]$Matches[3] + 1)
    }
    $VersionFile = Join-Path $Root "VERSION"
    if (-not (Test-Path -LiteralPath $VersionFile -PathType Leaf)) {
        throw "Canonical VERSION file was not found: $VersionFile"
    }
    $CurrentVersion = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
    $NextVersion = Get-NextPatchVersion $CurrentVersion
    $VersionSync = Join-Path $Root "scripts\sync_version.py"
    & $Python $VersionSync --set $NextVersion
    if ($LASTEXITCODE -ne 0) { throw "Version synchronization failed." }

    Write-Host "Build version bumped: $CurrentVersion -> $NextVersion"
}

$AppVersion = (Get-Content -LiteralPath (Join-Path $Root "VERSION") -Raw).Trim()
if (-not $AppVersion) { throw "VERSION does not define an application version" }
$ReleaseBuildId = ""
foreach ($envName in @("SONGAPP_BUILD_ID", "BUILD_ID", "GITHUB_SHA", "CI_COMMIT_SHA")) {
    $candidate = [Environment]::GetEnvironmentVariable($envName)
    if ($candidate) { $ReleaseBuildId = $candidate.Trim(); break }
}
if (-not $ReleaseBuildId) {
    try {
        $candidate = (& git -C $Root rev-parse HEAD 2>$null)
        if ($LASTEXITCODE -eq 0 -and $candidate) { $ReleaseBuildId = $candidate.Trim() }
    }
    catch {}
}
if (-not $ReleaseBuildId) { $ReleaseBuildId = "unknown" }
$AppExe = "A&D Voice.exe"

$ModelCheck = Join-Path $Backend "AI\install_models.py"
$InnoTemplate = Join-Path $Root "scripts\karaoke-studio.iss"
$ThemeIconsDir = Join-Path $Frontend "assets\icons"
$SignScript = Join-Path $Root "scripts\sign-windows.ps1"
$SetupIcon = Join-Path $Frontend "assets\icons\app.ico"

$InstallerExe = Join-Path $InstallerDir "A&D Voice Setup $AppVersion.exe"
$ChecksumFile = Join-Path $InstallerDir "SHA256SUMS.txt"
$IsoName = "A&D Voice $AppVersion.iso"
$IsoFile = Join-Path $Release $IsoName
$IsoView = Join-Path $Build "iso-view"
$LegacyIsoStage = Join-Path $Build "iso-root"
$IsoTemp = Join-Path $Build $IsoName
$PackagesDir = Join-Path $Build "packages"
$RuntimeArchive = Join-Path $PackagesDir "app-runtime.zip"

$SmokeScript = Join-Path $Root "scripts\smoke-packaged-backend.ps1"
$ChecksumScript = Join-Path $Root "scripts\generate-checksums.ps1"
$ManifestScript = Join-Path $Root "scripts\generate-release-manifest.ps1"
$ManifestFile = Join-Path $InstallerDir "release-manifest.json"
$ReleaseSbomScript = Join-Path $Root "scripts\generate_release_sbom.py"
$BackendSbomScript = Join-Path $Root "scripts\backend\generate_sbom.py"
$GeneratedSbomFile = Join-Path $Root "generated\sbom\release.cdx.json"
$SbomFile = Join-Path $InstallerDir "release.cdx.json"
$SizeReportScript = Join-Path $Root "scripts\generate-size-report.ps1"
$SizeReportFile = Join-Path $InstallerDir "size-report.json"
$script:BackendChanged = $false
$script:AsioChanged = $false
$script:FrontendChanged = $false
$script:ModelsChanged = $false
$script:BackendFingerprint = ""
$script:AsioFingerprint = ""
$script:FrontendFingerprint = ""
$script:ModelsFingerprint = ""
$script:StepRequiredOutputs = @{}
$LegacyV23SchemaVersion = "2026.08.11-v23-parallel-safe"

# Increment ONLY the component whose OUTPUT FORMAT/BUILD RULES changed.
# Never bump all of these just because build-installer.ps1 itself changed.
$BackendSchemaVersion   = "backend-v6-torch-only-monitor-onedir-v1"
$AsioSchemaVersion      = "asio-v1"
$FrontendSchemaVersion  = "frontend-v1"
$ModelsSchemaVersion    = "models-7z-v2"
$FinalizeSchemaVersion  = "finalize-v1"
$ElectronSchemaVersion  = "electron-v4-recoverable-downloads-optional-scene"
$RuntimeSchemaVersion   = "runtime-zip-v1"
$InstallerSchemaVersion = "installer-bootstrap-v3-mandatory-sbom"
$IsoSchemaVersion       = "iso-optional-models-v8-runtime-msst"
$IsoViewSchemaVersion   = "iso-view-hardlinks-v1"
$ElectronSignSchemaVersion  = "electron-sign-v1"
$ElectronSmokeSchemaVersion = "electron-smoke-v1"
$OutputManifestSchema = "build-output-manifest-v2-sha256-archive-check"

function Write-Header([string]$Text) {
    Write-Host ""
    Write-Host ("=" * 60)
    Write-Host " $Text"
    Write-Host ("=" * 60)
    Write-Host ""
}

function Write-BackendBuildIdentity {
    Require-Directory $BackendDist "Backend distribution"
    $identity = [ordered]@{ version = $AppVersion; build_id = $ReleaseBuildId }
    $identity | ConvertTo-Json | Set-Content `
        -LiteralPath (Join-Path $BackendDist "build-identity.json") `
        -Encoding UTF8
}

function Require-File([string]$Path, [string]$Name) {
    Write-Host "Checking ${Name}:"
    Write-Host "  $Path"

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Name was not found: $Path"
    }

    Write-Host "[OK] $Name"
    Write-Host ""
}

function Require-Directory([string]$Path, [string]$Name) {
    Write-Host "Checking ${Name}:"
    Write-Host "  $Path"

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Name was not found: $Path"
    }

    Write-Host "[OK] $Name"
    Write-Host ""
}

function Set-ElectronOutputPath([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)

    $script:Unpacked = $resolved
    $script:PackagedBackend = Join-Path $resolved "resources\backend"
    $script:PackagedSceneVideo = Join-Path $resolved "resources\media\videoplayback.webm"
}

function Set-InstallerOutputPath([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $script:InstallerDir = $resolved
    $script:InstallerExe = Join-Path $resolved ("A&D Voice Setup {0}.exe" -f $AppVersion)
    $script:ChecksumFile = Join-Path $resolved "SHA256SUMS.txt"
    $script:ManifestFile = Join-Path $resolved "release-manifest.json"
    $script:SbomFile = Join-Path $resolved "release.cdx.json"
    $script:SizeReportFile = Join-Path $resolved "size-report.json"
}

function Save-InstallerOutputPath([string]$Path) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $resolved = [IO.Path]::GetFullPath($Path)
    $resolved | Set-Content -LiteralPath $InstallerCurrentFile -Encoding UTF8
    Set-InstallerOutputPath $resolved
}

function Remove-OldInstallerRuns([string]$KeepPath) {
    if (-not (Test-Path -LiteralPath $InstallerRoot -PathType Container)) { return }

    $keep = ""
    if ($KeepPath) {
        try { $keep = [IO.Path]::GetFullPath($KeepPath).TrimEnd('\\') } catch {}
    }

    $runs = @(
        Get-ChildItem -LiteralPath $InstallerRoot -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "run-*" } |
        Sort-Object LastWriteTimeUtc -Descending
    )

    $keptExtra = 0
    foreach ($run in $runs) {
        $candidate = [IO.Path]::GetFullPath($run.FullName).TrimEnd('\\')
        if ($keep -and $candidate -eq $keep) { continue }
        if ($keptExtra -lt 1) { $keptExtra += 1; continue }

        try { Remove-Item -LiteralPath $run.FullName -Recurse -Force -ErrorAction Stop }
        catch { Write-Host "  Old installer run is locked; ignored: $($run.Name)" }
    }
}

function Save-ElectronOutputPath([string]$Path) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $resolved = [IO.Path]::GetFullPath($Path)
    $resolved | Set-Content -LiteralPath $ElectronCurrentFile -Encoding UTF8
    Set-ElectronOutputPath $resolved
}

function Remove-OldElectronRuns([string]$KeepPath) {
    if (-not (Test-Path -LiteralPath $ElectronRoot -PathType Container)) {
        return
    }

    $keep = ""
    if ($KeepPath) {
        try { $keep = [IO.Path]::GetFullPath($KeepPath).TrimEnd('\') } catch {}
    }

    $runs = @(
        Get-ChildItem -LiteralPath $ElectronRoot -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "run-*" } |
        Sort-Object LastWriteTimeUtc -Descending
    )

    foreach ($run in $runs) {
        $candidate = Join-Path $run.FullName "win-unpacked"
        $candidateFull = [IO.Path]::GetFullPath($candidate).TrimEnd('\')

        if ($keep -and $candidateFull -eq $keep) {
            continue
        }

        try {
            Remove-Item -LiteralPath $run.FullName -Recurse -Force -ErrorAction Stop
            Write-Host "  Removed old Electron run: $($run.Name)"
        }
        catch {
            Write-Host "  Old Electron run is locked; leaving it for later cleanup: $($run.Name)"
        }
    }

    # Legacy folder from old builder is also best-effort cleanup only.
    $legacy = Join-Path $ElectronRoot "win-unpacked"

    if (Test-Path -LiteralPath $legacy -PathType Container) {
        try {
            Remove-Item -LiteralPath $legacy -Recurse -Force -ErrorAction Stop
            Write-Host "  Removed legacy Electron output."
        }
        catch {
            Write-Host "  Legacy Electron output is locked; ignored."
        }
    }
}

function Test-ExcludedPath(
    [string]$FullName,
    [string[]]$ExcludeDirectoryNames = @(),
    [string[]]$ExcludeFilePatterns = @(),
    [string[]]$ExcludeRegexes = @()
) {
    $normalized = $FullName.Replace('/','\')

    foreach ($dir in $ExcludeDirectoryNames) {
        if ($normalized -match ('(?i)(^|\\)' + [Regex]::Escape($dir) + '(\\|$)')) {
            return $true
        }
    }

    $name = [IO.Path]::GetFileName($normalized)

    foreach ($pattern in $ExcludeFilePatterns) {
        if ($name -like $pattern) {
            return $true
        }
    }

    foreach ($regex in $ExcludeRegexes) {
        if ($normalized -match $regex) {
            return $true
        }
    }

    return $false
}

function Get-IncludedFiles(
    [string]$Root,
    [string[]]$ExcludeDirectoryNames = @(),
    [string[]]$ExcludeFilePatterns = @(),
    [string[]]$ExcludeRegexes = @()
) {
    $files = [Collections.Generic.List[IO.FileInfo]]::new()
    $pending = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $excludedDirectories = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )

    foreach ($name in $ExcludeDirectoryNames) {
        [void]$excludedDirectories.Add($name)
    }

    $pending.Push([IO.DirectoryInfo]::new($Root))
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()

        try {
            foreach ($file in $directory.EnumerateFiles()) {
                if (-not (Test-ExcludedPath $file.FullName @() $ExcludeFilePatterns $ExcludeRegexes)) {
                    $files.Add($file)
                }
            }

            foreach ($child in $directory.EnumerateDirectories()) {
                if ($excludedDirectories.Contains($child.Name)) { continue }
                if (Test-ExcludedPath $child.FullName @() @() $ExcludeRegexes) { continue }
                $pending.Push($child)
            }
        }
        catch [UnauthorizedAccessException] {
            continue
        }
        catch [IO.IOException] {
            continue
        }
    }

    return $files
}

function Get-Fingerprint(
    [string[]]$Paths,
    [string[]]$ExcludeDirectoryNames = @(),
    [string[]]$ExcludeFilePatterns = @(),
    [string[]]$ExcludeRegexes = @()
) {
    $rows = New-Object System.Collections.Generic.List[string]

    foreach ($path in $Paths) {
        if ([string]::IsNullOrWhiteSpace($path) -or -not (Test-Path -LiteralPath $path)) {
            $rows.Add("MISSING|$path")
            continue
        }

        $item = Get-Item -LiteralPath $path -Force

        if (-not $item.PSIsContainer) {
            if (-not (Test-ExcludedPath $item.FullName $ExcludeDirectoryNames $ExcludeFilePatterns $ExcludeRegexes)) {
                $rows.Add("F|$($item.FullName.ToLowerInvariant())|$($item.Length)|$($item.LastWriteTimeUtc.Ticks)")
            }
            continue
        }

        $root = [IO.Path]::GetFullPath($item.FullName).TrimEnd('\')

        foreach ($file in (Get-IncludedFiles $root $ExcludeDirectoryNames $ExcludeFilePatterns $ExcludeRegexes)) {
            $relative = $file.FullName.Substring($root.Length).TrimStart('\').ToLowerInvariant()
            $rows.Add("F|$root|$relative|$($file.Length)|$($file.LastWriteTimeUtc.Ticks)")
        }
    }

    $payload = [Text.Encoding]::UTF8.GetBytes((($rows | Sort-Object) -join "`n"))
    $sha = [Security.Cryptography.SHA256]::Create()

    try {
        return ([BitConverter]::ToString($sha.ComputeHash($payload))).Replace("-","").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-ContentFingerprint(
    [string[]]$Paths,
    [string[]]$ExcludeDirectoryNames = @(),
    [string[]]$ExcludeFilePatterns = @(),
    [string[]]$ExcludeRegexes = @()
) {
    $sha = [Security.Cryptography.SHA256]::Create()

    try {
        $files = New-Object System.Collections.Generic.List[object]

        foreach ($path in $Paths) {
            if ([string]::IsNullOrWhiteSpace($path) -or -not (Test-Path -LiteralPath $path)) {
                continue
            }

            $item = Get-Item -LiteralPath $path -Force

            if ($item.PSIsContainer) {
                foreach ($file in (Get-IncludedFiles $item.FullName $ExcludeDirectoryNames $ExcludeFilePatterns $ExcludeRegexes)) {
                    $files.Add($file)
                }
            }
            elseif (-not (Test-ExcludedPath $item.FullName $ExcludeDirectoryNames $ExcludeFilePatterns $ExcludeRegexes)) {
                $files.Add($item)
            }
        }

        foreach ($file in ($files | Sort-Object FullName)) {
            $pathBytes = [Text.Encoding]::UTF8.GetBytes($file.FullName.ToLowerInvariant() + "`n")
            [void]$sha.TransformBlock($pathBytes,0,$pathBytes.Length,$null,0)

            $stream = [IO.File]::OpenRead($file.FullName)

            try {
                $buffer = New-Object byte[] (1024 * 1024)

                while (($read = $stream.Read($buffer,0,$buffer.Length)) -gt 0) {
                    [void]$sha.TransformBlock($buffer,0,$read,$null,0)
                }
            }
            finally {
                $stream.Dispose()
            }
        }

        [void]$sha.TransformFinalBlock([byte[]]::new(0),0,0)
        return ([BitConverter]::ToString($sha.Hash)).Replace("-","").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-CombinedFingerprint([string[]]$Values) {
    $payload = [Text.Encoding]::UTF8.GetBytes(($Values -join "|"))
    $sha = [Security.Cryptography.SHA256]::Create()

    try {
        return ([BitConverter]::ToString($sha.ComputeHash($payload))).Replace("-","").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-StatePath([string]$Name) {
    return Join-Path $StateDir "$Name.sha256"
}

function Get-OutputStatePath([string]$Name) {
    return Join-Path $StateDir "$Name.outputs.json"
}

function Get-FileSha256([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-","").ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $sha.Dispose()
    }
}

function Test-ArchiveIntegrity([string]$Path) {
    if ([IO.Path]::GetExtension($Path).ToLowerInvariant() -ne ".zip") { return $true }
    $tar = $script:TarExe
    if (-not $tar) {
        $tar = Get-Command tar.exe -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Source -First 1
    }
    if (-not $tar) { return $false }
    & $tar -tf $Path *> $null
    return $LASTEXITCODE -eq 0
}

function Get-OutputManifestJson([string[]]$Paths) {
    $records = @()
    foreach ($path in @($Paths | Sort-Object -Unique)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $item = Get-Item -LiteralPath $path
            $records += [ordered]@{
                path = [IO.Path]::GetFullPath($path)
                size = $item.Length
                sha256 = Get-FileSha256 $path
            }
            continue
        }
        if (Test-Path -LiteralPath $path -PathType Container) {
            $rootPath = [IO.Path]::GetFullPath($path).TrimEnd('\')
            foreach ($file in Get-ChildItem -LiteralPath $path -File -Recurse | Sort-Object FullName) {
                $records += [ordered]@{
                    path = $file.FullName.Substring($rootPath.Length).TrimStart('\').Replace('\','/')
                    size = $file.Length
                    sha256 = Get-FileSha256 $file.FullName
                }
            }
            continue
        }
        $records += [ordered]@{ path = [IO.Path]::GetFullPath($path); missing = $true }
    }
    return ConvertTo-Json -InputObject ([ordered]@{
        schema = $OutputManifestSchema
        outputs = @($records)
    }) -Depth 5 -Compress
}

function Get-State([string]$Name) {
    $path = Get-StatePath $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return "" }
    return (Get-Content -LiteralPath $path -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
}

function Set-State([string]$Name, [string]$Fingerprint) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $Fingerprint | Set-Content -LiteralPath (Get-StatePath $Name) -Encoding ASCII
    $outputs = if ($script:StepRequiredOutputs.ContainsKey($Name)) {
        [string[]]$script:StepRequiredOutputs[$Name]
    }
    else { @() }
    foreach ($output in $outputs) {
        if ((Test-Path -LiteralPath $output -PathType Leaf) -and
            -not (Test-ArchiveIntegrity $output)) {
            throw "Cannot save build state for '$Name': archive integrity check failed: $output"
        }
    }
    Get-OutputManifestJson $outputs |
        Set-Content -LiteralPath (Get-OutputStatePath $Name) -Encoding UTF8
}

function Test-StepNeeded(
    [string]$Name,
    [string]$Fingerprint,
    [string[]]$RequiredOutputs = @(),
    [switch]$Force
) {
    $script:StepRequiredOutputs[$Name] = @($RequiredOutputs)
    if ($Force) {
        Write-Host "  $Name`: forced"
        return $true
    }

    foreach ($output in $RequiredOutputs) {
        if (-not (Test-Path -LiteralPath $output)) {
            Write-Host "  $Name`: output missing"
            return $true
        }
        if ((Test-Path -LiteralPath $output -PathType Leaf) -and
            -not (Test-ArchiveIntegrity $output)) {
            Write-Host "  $Name`: cached archive failed integrity check"
            return $true
        }
    }

    if ((Get-State $Name) -ne $Fingerprint) {
        Write-Host "  $Name`: changed"
        return $true
    }

    $outputStatePath = Get-OutputStatePath $Name
    if (-not (Test-Path -LiteralPath $outputStatePath -PathType Leaf)) {
        Write-Host "  $Name`: output integrity state missing"
        return $true
    }
    $savedOutputs = (Get-Content -LiteralPath $outputStatePath -Raw -ErrorAction SilentlyContinue).Trim()
    $actualOutputs = Get-OutputManifestJson $RequiredOutputs
    if ($savedOutputs -ne $actualOutputs) {
        Write-Host "  $Name`: cached output changed or corrupted"
        return $true
    }

    Write-Host "  $Name`: unchanged [skip]"
    return $false
}

function Migrate-StateIfCompatible(
    [string]$Name,
    [string]$NewFingerprint,
    [string[]]$LegacyFingerprints = @(),
    [string[]]$RequiredOutputs = @()
) {
    $script:StepRequiredOutputs[$Name] = @($RequiredOutputs)
    $saved = Get-State $Name

    if ($saved -eq $NewFingerprint) {
        return $false
    }

    if ([string]::IsNullOrWhiteSpace($saved)) {
        return $false
    }

    foreach ($output in $RequiredOutputs) {
        if (-not (Test-Path -LiteralPath $output)) {
            return $false
        }
    }

    foreach ($legacy in $LegacyFingerprints) {
        if (-not [string]::IsNullOrWhiteSpace($legacy) -and $saved -eq $legacy) {
            Set-State $Name $NewFingerprint
            Write-Host "  $Name`: compatible state migrated [skip]"
            return $true
        }
    }

    return $false
}

function Get-PythonEnvironmentFingerprint {
    $version = & $Python -c "import sys;print(sys.version)" 2>$null
    if ($LASTEXITCODE -ne 0) { return "python-unavailable" }

    $freeze = & $Python -m pip freeze --all --disable-pip-version-check 2>$null
    if ($LASTEXITCODE -ne 0) { $freeze = @() }

    return Get-CombinedFingerprint @(
        ($version -join "`n"),
        (($freeze | Sort-Object) -join "`n")
    )
}

function Get-ToolFileFingerprint([string[]]$Paths) {
    return Get-Fingerprint $Paths @() @() @()
}

function Get-InnoCompilerFingerprint {
    $inno = Find-Inno
    if (-not $inno) { return "inno-missing" }
    return Get-ToolFileFingerprint @($inno)
}

function Get-IsoEngineFingerprint {
    $osc = Find-Oscdimg
    if ($osc) { return Get-CombinedFingerprint @("oscdimg",(Get-ToolFileFingerprint @($osc))) }
    return "windows-imapi2fs"
}

function Get-BackendInputFingerprint {
    $source = Get-ContentFingerprint @($Backend) `
        @("venv","data","Song","full_songs","recordings","tests","__pycache__",".pytest_cache",".cache","coverage","htmlcov","dist","build") `
        @("*.pyc","*.pyo","*.log","*.db","*.sqlite","*.sqlite3",".coverage","requirements-dev.txt")

    $ffmpegFp = if ($script:Ffmpeg) {
        Get-ToolFileFingerprint @($script:Ffmpeg)
    }
    else {
        "ffmpeg-missing"
    }

    return Get-CombinedFingerprint @(
        $source,
        (Get-PythonEnvironmentFingerprint),
        $ffmpegFp
    )
}

function Get-BackendFingerprint {
    return Get-CombinedFingerprint @(
        (Get-BackendInputFingerprint),
        $BackendSchemaVersion
    )
}

function Get-FrontendInputFingerprint {
    $inputs = @(
        (Join-Path $Frontend "electron\rgb"),
        (Join-Path $Frontend "scripts\build-lighting.mjs"),
        (Join-Path $Frontend "src"),
        (Join-Path $Frontend "patches"),
        (Join-Path $Frontend "index.html"),
        (Join-Path $Frontend "package.json"),
        (Join-Path $Frontend "package-lock.json"),
        (Join-Path $Frontend "vite.config.mjs"),
        (Join-Path $Frontend ".babelrc")
    )

    return Get-ContentFingerprint $inputs `
        @("node_modules","dist","build",".git",".cache",".vite") `
        @("*.log")
}

function Get-FrontendFingerprint {
    return Get-CombinedFingerprint @(
        (Get-FrontendInputFingerprint),
        $FrontendSchemaVersion
    )
}

function Get-AsioInputFingerprint {
    $source = Get-ContentFingerprint @($Asio,$AsioSdk,(Join-Path $Backend "engines\wasapi")) `
        @("build",".git",".cache","__pycache__") `
        @("*.obj","*.pdb","*.ilk","*.log")

    return Get-CombinedFingerprint @(
        $source,
        (Get-ToolFileFingerprint @($VcVars,$CMake,$Ninja))
    )
}

function Get-AsioFingerprint {
    return Get-CombinedFingerprint @(
        (Get-AsioInputFingerprint),
        $AsioSchemaVersion
    )
}

function Get-ModelsInputFingerprint {
    $payload = Get-Fingerprint @($Models,$MsstEngine) `
        @(".cache",".git","__pycache__") `
        @("*.metadata","*.lock","*.tmp","*.part")

    $registryInputs = @($ModelCheck)

    foreach ($candidate in @(
        (Join-Path $Backend "AI\model_registry.py"),
        (Join-Path $Backend "AI\config.py"),
        (Join-Path $Backend "AI\requirements.txt")
    )) {
        if (Test-Path -LiteralPath $candidate) {
            $registryInputs += $candidate
        }
    }

    return Get-CombinedFingerprint @(
        $payload,
        (Get-SmallFileFingerprint $registryInputs)
    )
}

function Get-ModelsFingerprint {
    return Get-CombinedFingerprint @(
        (Get-ModelsInputFingerprint),
        $ModelsSchemaVersion
    )
}

function Get-SmallFileFingerprint([string[]]$Paths) {
    return Get-ContentFingerprint $Paths @() @() @()
}

function Get-ElectronConfigFingerprint {
    $paths = @()

    foreach ($candidate in @(
        (Join-Path $Frontend "package.json"),
        (Join-Path $Frontend "package-lock.json"),
        (Join-Path $Frontend "electron"),
        $MsstEngine,
        $SceneVideoSource
    )) {
        if (Test-Path -LiteralPath $candidate) {
            $paths += $candidate
        }
    }

    return Get-ContentFingerprint `
        $paths `
        @("node_modules","dist","build",".git",".cache") `
        @("*.log") `
        @()
}

function Get-InnoInputFingerprint {
    return Get-CombinedFingerprint @(
        (Get-SmallFileFingerprint @(
            $InnoTemplate,
            $ThemeIconsDir,
            $SetupIcon,
            $SignScript,
            $ChecksumScript,
            $ManifestScript,
            $SizeReportScript,
            $ReleaseSbomScript,
            $BackendSbomScript,
            (Join-Path $Frontend "scripts\generate-sbom.mjs"),
            (Join-Path $Backend "requirements-lock.txt"),
            (Join-Path $Frontend "package-lock.json"),
            (Join-Path $Root "cloudflare\package-lock.json")
        )),
        $AppName,
        $AppVersion,
        $AppExe,
        (Get-InnoCompilerFingerprint)
    )
}

function Get-InnoFingerprint {
    return Get-CombinedFingerprint @(
        (Get-InnoInputFingerprint),
        $InstallerSchemaVersion
    )
}

function Get-SigningFingerprint {
    $items = @($SignScript)

    if ($env:ADVOICE_SIGN_PFX -and
        (Test-Path -LiteralPath $env:ADVOICE_SIGN_PFX -PathType Leaf)) {
        $items += $env:ADVOICE_SIGN_PFX
    }

    return Get-CombinedFingerprint @(
        (Get-SmallFileFingerprint $items),
        [string]([bool]$env:ADVOICE_SIGN_PFX),
        [string]([bool]$env:ADVOICE_SIGN_PASSWORD)
    )
}

function Get-FinalizeFingerprint {
    return Get-CombinedFingerprint @(
        $script:BackendFingerprint,
        $script:AsioFingerprint,
        (Get-SigningFingerprint),
        $FinalizeSchemaVersion
    )
}

function Get-ElectronFingerprint {
    return Get-CombinedFingerprint @(
        $script:BackendFingerprint,
        $script:AsioFingerprint,
        $script:FrontendFingerprint,
        (Get-ElectronConfigFingerprint),
        $ElectronSchemaVersion
    )
}

function Get-RuntimeFingerprint(
    [string]$ElectronFingerprint,
    [string]$ElectronSignFingerprint = ""
) {
    $tar = if ($script:TarExe) {
        Get-ToolFileFingerprint @($script:TarExe)
    }
    else {
        "tar-missing"
    }

    return Get-CombinedFingerprint @(
        $ElectronFingerprint,
        $ElectronSignFingerprint,
        $tar,
        $RuntimeSchemaVersion
    )
}

function Get-InstallerFingerprint {
    # Bootstrap-only installer is independent of Electron/runtime/models.
    return Get-CombinedFingerprint @(
        (Get-InnoFingerprint),
        $InstallerSchemaVersion
    )
}

function Get-IsoFingerprint(
    [string]$InstallerFingerprint,
    [string]$RuntimeFingerprint
) {
    return Get-CombinedFingerprint @(
        $InstallerFingerprint,
        $RuntimeFingerprint,
        $AppVersion,
        (Get-IsoEngineFingerprint),
        $IsoViewSchemaVersion,
        $IsoSchemaVersion
    )
}

# Exact v23 hashes are retained ONLY to migrate an already successful build
# without rebuilding anything when upgrading the builder to v24.
function Get-LegacyV23FinalizeFingerprint(
    [string]$LegacyBackendFingerprint,
    [string]$LegacyAsioFingerprint
) {
    return Get-CombinedFingerprint @(
        $LegacyBackendFingerprint,
        $LegacyAsioFingerprint,
        (Get-SigningFingerprint),
        $LegacyV23SchemaVersion
    )
}

function Get-LegacyV23ElectronBaseFingerprint(
    [string]$LegacyBackendFingerprint,
    [string]$LegacyAsioFingerprint,
    [string]$LegacyFrontendFingerprint
) {
    return Get-CombinedFingerprint @(
        $LegacyBackendFingerprint,
        $LegacyAsioFingerprint,
        $LegacyFrontendFingerprint,
        (Get-ElectronConfigFingerprint),
        $LegacyV23SchemaVersion
    )
}

function Get-LegacyV23ElectronFingerprint(
    [string]$LegacyBackendFingerprint,
    [string]$LegacyAsioFingerprint,
    [string]$LegacyFrontendFingerprint,
    [string]$LegacyFinalizeFingerprint
) {
    return Get-CombinedFingerprint @(
        (Get-LegacyV23ElectronBaseFingerprint `
            $LegacyBackendFingerprint `
            $LegacyAsioFingerprint `
            $LegacyFrontendFingerprint),
        $LegacyFinalizeFingerprint
    )
}

function Get-LegacyV23InnoFingerprint {
    return Get-CombinedFingerprint @(
        (Get-InnoInputFingerprint),
        $LegacyV23SchemaVersion
    )
}

function Get-LegacyV23InstallerFingerprint {
    return Get-CombinedFingerprint @(
        (Get-LegacyV23InnoFingerprint),
        $LegacyV23SchemaVersion
    )
}

function Get-LegacyV23IsoFingerprint(
    [string]$LegacyInstallerFingerprint,
    [string]$LegacyElectronFingerprint,
    [string]$LegacyModelsFingerprint
) {
    return Get-CombinedFingerprint @(
        $LegacyInstallerFingerprint,
        $LegacyElectronFingerprint,
        $LegacyModelsFingerprint,
        $AppVersion,
        (Get-IsoEngineFingerprint),
        $LegacyV23SchemaVersion
    )
}

function Format-Duration([double]$Seconds) {
    if ($Seconds -lt 0) { return "--:--" }

    $ts = [TimeSpan]::FromSeconds([Math]::Floor($Seconds))

    if ($ts.TotalHours -ge 1) {
        return "{0:00}:{1:00}:{2:00}" -f [int]$ts.TotalHours,$ts.Minutes,$ts.Seconds
    }

    return "{0:00}:{1:00}" -f $ts.Minutes,$ts.Seconds
}

function Write-StepEstimate([string]$Name) {
    $previous = Get-PreviousDuration $Name
    if ($previous -gt 0) {
        Write-Host ("  Estimated from previous build: ~{0}" -f (Format-Duration $previous))
    }
    else {
        Write-Host "  Estimated time: learning from this build"
    }
}

function Get-TimingPath([string]$Name) {
    return Join-Path $TimingDir "$Name.seconds"
}

function Get-PreviousDuration([string]$Name) {
    $path = Get-TimingPath $Name

    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return 0.0
    }

    $raw = Get-Content -LiteralPath $path -ErrorAction SilentlyContinue | Select-Object -First 1
    $value = 0.0

    if ([double]::TryParse(
        $raw,
        [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$value
    )) {
        return $value
    }

    return 0.0
}

function Set-PreviousDuration([string]$Name, [double]$Seconds) {
    New-Item -ItemType Directory -Path $TimingDir -Force | Out-Null

    $Seconds.ToString(
        "0.000",
        [Globalization.CultureInfo]::InvariantCulture
    ) | Set-Content -LiteralPath (Get-TimingPath $Name) -Encoding ASCII
}

function Write-StepProgress(
    [string]$Name,
    [double]$ElapsedSeconds,
    [double]$ExpectedSeconds = 0,
    [string]$Status = "RUNNING"
) {
    $elapsedText = Format-Duration $ElapsedSeconds

    if ($Status -ne "RUNNING") {
        Write-Host ("  [{0}] {1}: {2} | elapsed {3}" -f $Status,$Name,$Status,$elapsedText)
        return
    }

    if ($ExpectedSeconds -gt 1) {
        $pct = [Math]::Min(99, [Math]::Max(1, [int](100 * $ElapsedSeconds / $ExpectedSeconds)))
        $remaining = [Math]::Max(0, $ExpectedSeconds - $ElapsedSeconds)
        $etaText = Format-Duration $remaining

        Write-Host ("  [PROGRESS] {0}: {1}% | elapsed {2} | ETA ~{3}" -f $Name,$pct,$elapsedText,$etaText)
    }
    else {
        Write-Host ("  [PROGRESS] {0}: elapsed {1} | ETA learning from this build" -f $Name,$elapsedText)
    }
}

function Invoke-TimedStep(
    [string]$Name,
    [scriptblock]$Action
) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Write-Host ""
    Write-Host ("[START] {0}" -f $Name)

    try {
        & $Action
    }
    finally {
        $sw.Stop()
    }

    Set-PreviousDuration $Name $sw.Elapsed.TotalSeconds

    Write-Host ("[DONE] {0} | elapsed {1}" -f $Name,(Format-Duration $sw.Elapsed.TotalSeconds))
}

function Stop-ProcessesUsingPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }

    $target = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $targetLower = $target.ToLowerInvariant()
    $currentPid = $PID

    Write-Host "Checking processes that may lock:"
    Write-Host "  $target"

    $found = $false

    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            ForEach-Object {
                if ($_.ProcessId -eq $currentPid) {
                    return
                }

                $exe = ""
                $cmd = ""

                try { $exe = [string]$_.ExecutablePath } catch {}
                try { $cmd = [string]$_.CommandLine } catch {}

                $match = $false

                if ($exe) {
                    try {
                        $exeFull = [IO.Path]::GetFullPath($exe).ToLowerInvariant()
                        if ($exeFull.StartsWith($targetLower + "\") -or $exeFull -eq $targetLower) {
                            $match = $true
                        }
                    }
                    catch {
                    }
                }

                if (-not $match -and $cmd) {
                    if ($cmd.ToLowerInvariant().Contains($targetLower)) {
                        $match = $true
                    }
                }

                if ($match) {
                    $found = $true
                    Write-Host ("  Stopping PID {0}: {1}" -f $_.ProcessId,$_.Name)

                    try {
                        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
                    }
                    catch {
                        try {
                            & taskkill.exe /PID $_.ProcessId /T /F *> $null
                        }
                        catch {
                        }
                    }
                }
            }
    }
    catch {
    }

    if ($found) {
        Start-Sleep -Milliseconds 800
    }
    else {
        Write-Host "  No matching process found."
    }
}

function Clear-ReadOnlyAttributes([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    try {
        & attrib.exe -R "$Path\*" /S /D *> $null
    }
    catch {
    }
}

function Remove-DirectoryWithRobocopyFallback([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return
    }

    $empty = Join-Path $env:TEMP ("advoice-empty-" + [Guid]::NewGuid().ToString("N"))

    try {
        New-Item -ItemType Directory -Path $empty -Force | Out-Null

        & robocopy.exe $empty $Path /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP *> $null
        $rc = $LASTEXITCODE

        if ($rc -lt 8) {
            try {
                Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            }
            catch {
            }
        }
    }
    finally {
        Remove-Item -LiteralPath $empty -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Remove-Directory {
    param(
        [string]$Path,
        [switch]$AllowLockedRemainder
    )
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Refusing to remove an empty path."
    }

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $fullPath = [IO.Path]::GetFullPath($Path)

    Write-Host "Removing:"
    Write-Host "  $fullPath"

    Stop-ProcessesUsingPath $fullPath
    Clear-ReadOnlyAttributes $fullPath

    for ($i = 1; $i -le 6; $i++) {
        try {
            Remove-Item -LiteralPath $fullPath -Recurse -Force -ErrorAction Stop
        }
        catch {
        }

        if (-not (Test-Path -LiteralPath $fullPath)) {
            return
        }

        if ($i -eq 2 -or $i -eq 4) {
            Stop-ProcessesUsingPath $fullPath
            Clear-ReadOnlyAttributes $fullPath
        }

        if ($i -eq 5) {
            Write-Host "  Standard deletion still blocked. Trying robocopy cleanup..."
            Remove-DirectoryWithRobocopyFallback $fullPath
        }

        if (-not (Test-Path -LiteralPath $fullPath)) {
            return
        }

        Write-Host "  Directory is still locked. Retry $i/6..."
        Start-Sleep -Seconds 1
    }

    # Last diagnostic pass
    Stop-ProcessesUsingPath $fullPath

    $lockedItems = @()
    try {
        $lockedItems = @(
            Get-ChildItem -LiteralPath $fullPath -Recurse -Force -ErrorAction SilentlyContinue |
            Select-Object -First 10 -ExpandProperty FullName
        )
    }
    catch {
    }

    Write-Host ""
    Write-Host "[ERROR] Directory could not be removed:"
    Write-Host "  $fullPath"

    if ($lockedItems.Count -gt 0) {
        Write-Host "Remaining files/directories:"
        foreach ($item in $lockedItems) {
            Write-Host "  $item"
        }
    }

    if ($AllowLockedRemainder) {
        Write-Warning "Locked stale build files were left in place. The clean build will use fresh output paths."
        return
    }

    throw "Could not remove directory: $fullPath"
}

function Stop-BuildProcesses {
    param([switch]$Quiet)

    if (-not $Quiet) {
        Write-Host ""
        Write-Host "[0/7] Closing old A&D Voice build processes..."
    }

    $stoppedAny = $false

    foreach ($name in @("A&D Voice","KaraokeBackend","KaraokeAsioBridge")) {
        $targets = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
        foreach ($target in $targets) {
            Stop-Process -Id $target.Id -Force -ErrorAction SilentlyContinue
            $stoppedAny = $true
        }
    }

    if (Test-Path -LiteralPath $Release) {
        $releaseFull = [IO.Path]::GetFullPath($Release)

        Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                if ($_.Path) {
                    $full = [IO.Path]::GetFullPath($_.Path)

                    if ($full.StartsWith($releaseFull, [StringComparison]::OrdinalIgnoreCase)) {
                        if (-not $Quiet) {
                            Write-Host "  Closing PID $($_.Id): $($_.ProcessName)"
                        }

                        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
                        $stoppedAny = $true
                    }
                }
            }
            catch {
            }
        }
    }

    # Kill only node/electron/electron-builder processes that belong to this project.
    $projectLower = [IO.Path]::GetFullPath($Root).TrimEnd('\').ToLowerInvariant()

    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ProcessId -ne $PID -and
                $_.Name -match '^(node|electron|electron-builder|7za)\.exe$' -and
                $_.CommandLine -and
                $_.CommandLine.ToLowerInvariant().Contains($projectLower)
            } |
            ForEach-Object {
                if (-not $Quiet) {
                    Write-Host ("  Closing project process PID {0}: {1}" -f $_.ProcessId,$_.Name)
                }

                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                $stoppedAny = $true
            }
    }
    catch {
    }

    if ($stoppedAny) { Start-Sleep -Milliseconds 300 }
}

function Get-RequiredCommand([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1

    if (-not $cmd) {
        throw "$Name was not found in PATH."
    }

    return $cmd.Source
}

function Find-Oscdimg {
    if ($env:OSCDIMG_OVERRIDE -and (Test-Path -LiteralPath $env:OSCDIMG_OVERRIDE -PathType Leaf)) {
        return [IO.Path]::GetFullPath($env:OSCDIMG_OVERRIDE)
    }

    $cmd = Get-Command oscdimg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }

    foreach ($candidate in @(
        (Join-Path ${env:ProgramFiles(x86)} "Windows Kits\\10\\Assessment and Deployment Kit\\Deployment Tools\\amd64\\Oscdimg\\oscdimg.exe"),
        (Join-Path $env:ProgramFiles "Windows Kits\\10\\Assessment and Deployment Kit\\Deployment Tools\\amd64\\Oscdimg\\oscdimg.exe")
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }

    return $null
}

function Initialize-ImapiStreamWriter {
    if ("AdVoice.ImapiStreamWriter" -as [type]) {
        return
    }

    Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Runtime.InteropServices;

namespace AdVoice
{
    public static class ImapiStreamWriter
    {
        private static readonly Guid IID_IStream =
            new Guid("0000000C-0000-0000-C000-000000000046");

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int ReadDelegate(
            IntPtr thisPtr,
            [Out] byte[] buffer,
            int count,
            IntPtr bytesRead
        );

        public static void CopyToFile(object comStream, string path)
        {
            if (comStream == null)
                throw new ArgumentNullException("comStream");

            IntPtr unknown = IntPtr.Zero;
            IntPtr streamPtr = IntPtr.Zero;
            IntPtr bytesReadPtr = IntPtr.Zero;

            try
            {
                unknown = Marshal.GetIUnknownForObject(comStream);

                Guid iid = IID_IStream;
                int hr = Marshal.QueryInterface(unknown, ref iid, out streamPtr);

                if (hr != 0 || streamPtr == IntPtr.Zero)
                    Marshal.ThrowExceptionForHR(hr);

                // IStream inherits ISequentialStream:
                // vtable[0..2] = IUnknown
                // vtable[3]    = ISequentialStream::Read
                IntPtr vtable = Marshal.ReadIntPtr(streamPtr);
                IntPtr readAddress = Marshal.ReadIntPtr(
                    vtable,
                    IntPtr.Size * 3
                );

                ReadDelegate read = (ReadDelegate)
                    Marshal.GetDelegateForFunctionPointer(
                        readAddress,
                        typeof(ReadDelegate)
                    );

                byte[] buffer = new byte[4 * 1024 * 1024];
                bytesReadPtr = Marshal.AllocHGlobal(sizeof(int));

                using (FileStream output = new FileStream(
                    path,
                    FileMode.Create,
                    FileAccess.Write,
                    FileShare.None,
                    buffer.Length,
                    FileOptions.SequentialScan))
                {
                    while (true)
                    {
                        Marshal.WriteInt32(bytesReadPtr, 0);

                        hr = read(
                            streamPtr,
                            buffer,
                            buffer.Length,
                            bytesReadPtr
                        );

                        // S_OK (0) and S_FALSE (1) are both valid for Read.
                        if (hr < 0)
                            Marshal.ThrowExceptionForHR(hr);

                        int readCount = Marshal.ReadInt32(bytesReadPtr);

                        if (readCount <= 0)
                            break;

                        output.Write(buffer, 0, readCount);
                    }

                    output.Flush(true);
                }
            }
            finally
            {
                if (bytesReadPtr != IntPtr.Zero)
                    Marshal.FreeHGlobal(bytesReadPtr);

                if (streamPtr != IntPtr.Zero)
                    Marshal.Release(streamPtr);

                if (unknown != IntPtr.Zero)
                    Marshal.Release(unknown);
            }
        }
    }
}
"@
}

function New-IsoWithWindowsImapi(
    [string]$SourceDirectory,
    [string]$OutputFile,
    [string]$VolumeName
) {
    Require-Directory $SourceDirectory "ISO source directory"

    Write-Host "ISO engine:"
    Write-Host "  Windows IMAPI2FS (built in)"
    Write-Host ""

    Initialize-ImapiStreamWriter

    $fsi = $null
    $result = $null
    $stream = $null

    try {
        $fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage

        # 4 = UDF. UDF is required because AI model files may exceed 4 GB.
        $fsi.FileSystemsToCreate = 4
        $fsi.VolumeName = $VolumeName

        # 0 = unlimited image size. Without this IMAPI assumes optical-media
        # capacity and rejects large offline distributions.
        $fsi.FreeMediaBlocks = 0

        try {
            # UDF 1.02 offers broad Windows compatibility.
            $fsi.UDFRevision = 0x0102
        }
        catch {
            # Some Windows builds do not expose this setter. The default UDF
            # revision is still suitable, so this is intentionally non-fatal.
        }

        Write-Host "Adding files to ISO image..."
        $fsi.Root.AddTree($SourceDirectory, $false)

        Write-Host "Generating UDF image stream..."
        $result = $fsi.CreateResultImage()
        $stream = $result.ImageStream

        if (Test-Path -LiteralPath $OutputFile -PathType Leaf) {
            Remove-Item -LiteralPath $OutputFile -Force
        }

        Write-Host "Writing ISO via native COM IStream reader:"
        Write-Host "  $OutputFile"
        Write-Host ""

        [AdVoice.ImapiStreamWriter]::CopyToFile(
            $stream,
            $OutputFile
        )
    }
    catch {
        throw "Windows IMAPI ISO creation failed: $($_.Exception.Message)"
    }
    finally {
        # Do NOT call FinalReleaseComObject here. IMAPI objects may share RCWs;
        # forcing an RCW reference count to zero can detach another live wrapper.
        $stream = $null
        $result = $null
        $fsi = $null

        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
        [GC]::Collect()
    }

    Require-File $OutputFile "Distribution ISO"
}

function New-DistributionIsoImage(
    [string]$SourceDirectory,
    [string]$OutputFile,
    [string]$VolumeName
) {
    if ($script:Oscdimg) {
        Write-Host "ISO engine:"
        Write-Host "  Microsoft Oscdimg"
        Write-Host "  $script:Oscdimg"
        Write-Host ""

        $isoProcess = Start-Process `
            -FilePath $script:Oscdimg `
            -ArgumentList @(
                "-m",
                "-u2",
                "-udfver102",
                "-l$VolumeName",
                $SourceDirectory,
                $OutputFile
            ) `
            -PassThru `
            -Wait `
            -NoNewWindow

        if ($isoProcess.ExitCode -ne 0) {
            throw "Oscdimg failed. Exit code: $($isoProcess.ExitCode)"
        }

        Require-File $OutputFile "Distribution ISO"
        return
    }

    New-IsoWithWindowsImapi `
        -SourceDirectory $SourceDirectory `
        -OutputFile $OutputFile `
        -VolumeName $VolumeName
}

function Get-DirectoryBytes([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return [int64]0 }
    [int64]$total = 0
    Get-ChildItem -LiteralPath $Path -File -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object { $total += [int64]$_.Length }
    return $total
}

function Assert-BuildStorageBudget {
    $target = [IO.Path]::GetFullPath($Build)
    $rootPath = [IO.Path]::GetPathRoot($target)
    $drive = [IO.DriveInfo]::new($rootPath)
    [int64]$inputBytes = (Get-DirectoryBytes (Join-Path $Backend "venv")) +
        (Get-DirectoryBytes $Models)
    [int64]$floor = if ($Mode -in @("installer", "setup")) { 4GB } else { 8GB }
    [int64]$required = [Math]::Max($floor, ($inputBytes * 2) + 2GB)
    [int64]$free = $drive.AvailableFreeSpace
    Write-Host "Build storage preflight:"
    Write-Host "  Required scratch: $([Math]::Round($required / 1GB, 2)) GB"
    Write-Host "  Free on $rootPath`: $([Math]::Round($free / 1GB, 2)) GB"
    Write-Host ""
    if ($free -lt $required) {
        throw "Insufficient build storage: required=$required bytes, free=$free bytes, target=$target"
    }
}

function Check-Environment {
    Write-Host ""
    Write-Host "[0/7] Checking build environment..."
    Write-Host ""

    Assert-BuildStorageBudget

    Require-Directory $Backend "Backend directory"
    Require-Directory $Frontend "Frontend directory"
    Require-File $InnoTemplate "Inno Setup template"

    $script:TarExe = Get-Command tar.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
    if (-not $script:TarExe) {
        throw "Windows tar.exe was not found. Windows 10/11 built-in tar is required for app-runtime.zip."
    }
    Write-Host "Checking runtime archive tool:"
    Write-Host "  $script:TarExe"
    Write-Host "[OK] Windows tar.exe"
    Write-Host ""

    $script:Oscdimg = Find-Oscdimg

    Write-Host "Checking ISO builder:"
    if ($script:Oscdimg) {
        Write-Host "  Microsoft Oscdimg:"
        Write-Host "  $script:Oscdimg"
        Write-Host "[OK] ISO builder: Oscdimg"
        Write-Host ""
    }
    else {
        Write-Host "  Microsoft Oscdimg: not installed"
        Write-Host "  Fallback: Windows IMAPI2FS"
        Write-Host "[OK] ISO builder: built-in Windows IMAPI fallback"
        Write-Host ""
    }

    if ($Mode -in @("installer","setup")) {
        return
    }

    Require-File $Python "Backend virtual environment Python"

    if ($Mode -eq "fast") {
        Verify-BackendBase
        return
    }

    Require-File $VcVars "Visual C++ Build Tools"
    Require-File $CMake "Visual Studio CMake"
    Require-File $Ninja "Visual Studio Ninja"
    Require-Directory $Asio "ASIO source directory"
    Require-Directory $AsioSdk "ASIO SDK directory"
    Require-File $ModelCheck "AI model verification script"

    $script:NodeExe = Get-RequiredCommand "node.exe"
    $script:NpmCmd = Get-RequiredCommand "npm.cmd"
    $script:NpxCmd = Get-RequiredCommand "npx.cmd"
    $script:Ffmpeg = Get-RequiredCommand "ffmpeg.exe"

    Write-Host "Checking node.exe:"
    Write-Host "  $script:NodeExe"
    $nodeVersionText = (& $script:NodeExe -p "process.versions.node").Trim()
    try { $nodeVersion = [version]$nodeVersionText } catch { throw "Could not parse Node.js version: $nodeVersionText" }
    $nodeSupported = (($nodeVersion.Major -eq 22 -and $nodeVersion -ge [version]"22.18.0") -or ($nodeVersion.Major -eq 24 -and $nodeVersion -ge [version]"24.11.0") -or $nodeVersion.Major -gt 24)
    if (-not $nodeSupported) { throw "Unsupported Node.js $nodeVersionText. Required: >=22.18.0 <23 or >=24.11.0." }
    Write-Host "[OK] node.exe $nodeVersionText"
    Write-Host ""

    Write-Host "Checking npm.cmd:"
    Write-Host "  $script:NpmCmd"
    Write-Host "[OK] npm.cmd"
    Write-Host ""

    Write-Host "Checking ffmpeg.exe:"
    Write-Host "  $script:Ffmpeg"
    Write-Host "[OK] ffmpeg.exe"
    Write-Host ""

    Write-Host "Python:"
    Write-Host "  $Python"
    Write-Host ""
    Write-Host "FFmpeg:"
    Write-Host "  $script:Ffmpeg"
    Write-Host ""
}

function Prepare-Output {
    Write-Host ""
    Write-Host "[0/7] Preparing smart incremental build..."

    if ($Mode -eq "clean") {
        Write-Host ""
        Write-Host "Performing complete clean build..."
        Write-Host ""

        Remove-Directory $Release
        # Old Electron test/run directories can be held briefly by Windows,
        # antivirus or an already closing renderer. They do not overlap the
        # clean build's fresh output paths and must not abort the whole release.
        Remove-Directory $Build -AllowLockedRemainder

        Set-ElectronOutputPath (Join-Path $ElectronRoot "win-unpacked")
        Set-InstallerOutputPath (Join-Path $InstallerRoot "current")

        $pyInstallerCache = Join-Path $env:LOCALAPPDATA "pyinstaller"
        Remove-Directory $pyInstallerCache
    }

    New-Item -ItemType Directory -Path $Build -Force | Out-Null
    New-Item -ItemType Directory -Path $Release -Force | Out-Null
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    New-Item -ItemType Directory -Path $InstallerRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $PackagesDir -Force | Out-Null

    # Old versions physically copied 10-20 GB into build\iso-root.
    # Delete that obsolete duplicate once. The new iso-view contains links only.
    if (Test-Path -LiteralPath $LegacyIsoStage) {
        Write-Host ""
        Write-Host "Removing obsolete physical ISO staging:"
        Write-Host "  $LegacyIsoStage"
        Remove-Directory $LegacyIsoStage
    }

    New-Item -ItemType Directory -Path $IsoView -Force | Out-Null
}

function Check-Models {
    Write-Host ""
    Write-Host "[1/7] Checking all registered offline AI models..."

    $old = $env:PYTHONPATH

    try {
        if ($old) {
            $env:PYTHONPATH = "$Backend;$old"
        }
        else {
            $env:PYTHONPATH = $Backend
        }

        Push-Location $Backend
        try {
            & $Python -m AI.install_models `
                --downloads $Downloads `
                --msst $MsstEngine `
                --env (Join-Path $Downloads "ai-environment.bat") `
                --check
        }
        finally {
            Pop-Location
        }

        if ($LASTEXITCODE -ne 0) {
            throw "Offline model verification failed."
        }
    }
    finally {
        $env:PYTHONPATH = $old
    }
}

function Ensure-PyInstaller {
    $checkOut = Join-Path $env:TEMP "advoice-pyinstaller-check.out"
    $checkErr = Join-Path $env:TEMP "advoice-pyinstaller-check.err"

    Remove-Item -LiteralPath $checkOut,$checkErr -Force -ErrorAction SilentlyContinue

    $check = Start-Process `
        -FilePath $Python `
        -ArgumentList @("-m","PyInstaller","--version") `
        -RedirectStandardOutput $checkOut `
        -RedirectStandardError $checkErr `
        -PassThru `
        -Wait `
        -NoNewWindow

    if ($check.ExitCode -ne 0) {
        Write-Host ""
        Write-Host "PyInstaller is not installed. Installing..."
        Write-Host ""

        $install = Start-Process `
            -FilePath $Python `
            -ArgumentList @("-m","pip","install","pyinstaller") `
            -PassThru `
            -Wait `
            -NoNewWindow

        if ($install.ExitCode -ne 0) {
            throw "PyInstaller installation failed. Exit code: $($install.ExitCode)"
        }

        Remove-Item -LiteralPath $checkOut,$checkErr -Force -ErrorAction SilentlyContinue

        $check = Start-Process `
            -FilePath $Python `
            -ArgumentList @("-m","PyInstaller","--version") `
            -RedirectStandardOutput $checkOut `
            -RedirectStandardError $checkErr `
            -PassThru `
            -Wait `
            -NoNewWindow

        if ($check.ExitCode -ne 0) {
            if (Test-Path -LiteralPath $checkErr) {
                Get-Content -LiteralPath $checkErr
            }
            throw "PyInstaller is still unavailable after installation."
        }
    }

    $version = ""
    if (Test-Path -LiteralPath $checkOut) {
        $version = (Get-Content -LiteralPath $checkOut -ErrorAction SilentlyContinue | Select-Object -First 1)
    }

    if ([string]::IsNullOrWhiteSpace($version)) {
        $version = "installed"
    }

    Write-Host "PyInstaller version: $version"

    Remove-Item -LiteralPath $checkOut,$checkErr -Force -ErrorAction SilentlyContinue
}

function Build-Backend {
    Write-Host ""
    Write-Host "[2/7] Building Python executables..."

    Ensure-PyInstaller

    Remove-Directory (Join-Path $Build "backend\dist")
    New-Item -ItemType Directory -Path (Join-Path $Build "backend\dist") -Force | Out-Null

    Push-Location $Backend

    try {
        Write-Host ""
        Write-Host "Building KaraokeBackend.exe..."
        Write-Host ""

        $args = @(
            "-m","PyInstaller",
            "--log-level","ERROR",
            "--noconfirm"
        )

        if ($Mode -eq "clean") {
            $args += "--clean"
        }

        # nagisa 0.2.x imports its Cython extensions as top-level modules
        # (for example ``import prepro``), but those extensions are distributed
        # as files owned by the nagisa wheel and are not guaranteed to be
        # discoverable via importlib.util.find_spec("prepro") in the build venv.
        # Resolve the actual wheel files from importlib.metadata instead and
        # copy them to the frozen application root, where the absolute imports
        # used by nagisa.train can resolve them.
        $nagisaNative = @{}
        foreach ($moduleName in @("prepro", "nagisa_utils")) {
            $modulePath = (& $Python -c "from importlib.metadata import files; from pathlib import Path; name='$moduleName'; hits=[Path(p.locate()).resolve() for p in (files('nagisa') or []) if Path(str(p)).name.startswith(name) and Path(str(p)).suffix.lower() in ('.pyd','.dll')]; print(hits[0] if hits else '')").Trim()
            if ($LASTEXITCODE -ne 0) {
                throw "Could not inspect Nagisa package files while resolving '$moduleName'."
            }
            if (-not [string]::IsNullOrWhiteSpace($modulePath) -and (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
                $nagisaNative[$moduleName] = $modulePath
                Write-Host "Nagisa native module: $moduleName -> $modulePath"
            }
            else {
                Write-Warning "Nagisa native module '$moduleName' was not listed as a standalone wheel binary. PyInstaller --collect-all nagisa will be used and the packaged Qwen smoke test will verify the result."
            }
        }

        $args += @(
            "--onedir",
            "--name","KaraokeBackend",
            "--distpath",(Join-Path $Build "backend\dist"),
            "--workpath",(Join-Path $Build "backend\pyinstaller\KaraokeBackend"),
            "--specpath",(Join-Path $Build "backend\spec"),
            "--paths",(Join-Path $Backend "AI"),
            "--paths",$MsstEngine,
            "--add-data","$(Join-Path $Backend 'AI');AI",
            "--add-binary","$script:Ffmpeg;.",
            "--hidden-import","run_all",
            "--hidden-import","app.services.monitor_worker",
            "--collect-submodules","omegaconf",
            "--collect-submodules","ml_collections",
            "--collect-submodules","beartype",
            "--collect-submodules","rotary_embedding_torch",
            "--collect-all","qwen_asr",
            "--collect-all","nagisa",
            "--collect-all","yt_dlp",
            "--collect-all","yt_dlp_ejs",
            "--collect-data","torchfcpe",
            "--exclude-module","tkinter",
            "--exclude-module","_tkinter",
            "--exclude-module","idlelib",
            "--exclude-module","turtledemo",
            "--exclude-module","tensorflow",
            "--exclude-module","tf_keras",
            "--exclude-module","keras",
            "--exclude-module","jax",
            "--exclude-module","flax",
            "run.py"
        )

        foreach ($moduleName in @("prepro", "nagisa_utils")) {
            if ($nagisaNative.ContainsKey($moduleName)) {
                $args += @("--add-binary", "$($nagisaNative[$moduleName]);.")
            }
        }

        & $Python @args

        if ($LASTEXITCODE -ne 0) {
            throw "KaraokeBackend PyInstaller build failed."
        }

    }
    finally {
        Pop-Location
    }

    Remove-LegacyEmbeddedAI
    Require-File (Join-Path $BackendDist "KaraokeBackend.exe") "KaraokeBackend.exe"
}

function Build-Asio {
    Write-Host ""
    Write-Host "[3/7] Compiling native ASIO bridge..."

    New-Item -ItemType Directory -Path $AsioBuild -Force | Out-Null

    $cmd = @"
call "$VcVars" >nul && "$CMake" -S "$Asio" -B "$AsioBuild" -G Ninja -DCMAKE_BUILD_TYPE=Release -DASIO_SDK_DIR="$AsioSdk" -DCMAKE_MAKE_PROGRAM="$Ninja" && "$CMake" --build "$AsioBuild" --parallel
"@

    & cmd.exe /D /S /C $cmd

    if ($LASTEXITCODE -ne 0) {
        throw "ASIO compilation failed."
    }

    Require-File (Join-Path $AsioBuild "KaraokeAsioBridge.exe") "Compiled KaraokeAsioBridge.exe"
    Require-File (Join-Path $AsioBuild "KaraokeWasapi.dll") "Compiled shared WASAPI library"
}

function Build-VirtualAudio {
    Write-Host ""
    Write-Host "Compiling A&D Voice virtual microphone driver..."
    Require-File $VirtualAudioPrepare "Virtual audio preparation script"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $VirtualAudioPrepare -Root $Root
    if ($LASTEXITCODE -ne 0) { throw "ADVoiceVirtualAudio compilation failed." }

    $driver = Join-Path $VirtualAudioPackage "SimpleAudioSample.sys"
    $catalog = Join-Path $VirtualAudioPackage "SimpleAudioSample.cat"
    $inf = Join-Path $VirtualAudioPackage "ADVoiceVirtualAudio.inf"
    Require-File $driver "ADVoiceVirtualAudio kernel driver"
    Require-File $catalog "ADVoiceVirtualAudio catalog"
    Require-File $inf "ADVoiceVirtualAudio INF"

    # The binary is signed before regenerating the catalog because the catalog
    # contains the binary hash. The finished catalog is signed last.
    Sign-File $driver
    $inf2cat = Get-ChildItem -LiteralPath (Join-Path $Downloads "engines\windows-driver-samples\packages") `
        -Recurse -Filter "Inf2Cat.exe" | Select-Object -First 1
    if (-not $inf2cat) { throw "Inf2Cat was not restored with the virtual audio build dependencies." }
    & $inf2cat.FullName "/driver:$VirtualAudioPackage" /os:10_X64
    if ($LASTEXITCODE -ne 0) { throw "ADVoiceVirtualAudio catalog generation failed." }
    Sign-File $catalog

    $destination = Join-Path $BackendDist "drivers\ADVoiceVirtualAudio"
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Copy-Item -Path (Join-Path $VirtualAudioPackage "*") -Destination $destination -Force
}

function Sign-File([string]$Path) {
    Require-File $SignScript "Signing script"

    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $SignScript, "-Path", $Path)
    # A verified clean release must be signed. The explicit no-checks developer
    # entry point intentionally permits a clean unsigned build for local testing.
    if ($Mode -eq "clean" -and -not $SkipReleaseGate) { $arguments += "-Required" }
    & powershell.exe @arguments

    if ($LASTEXITCODE -ne 0) {
        throw "Code signing failed for: $Path"
    }
}

function Finalize-Asio {
    Write-Host ""
    Write-Host "Finalizing ASIO bridge and signing backend executables..."

    $bridge = Join-Path $AsioBuild "KaraokeAsioBridge.exe"

    Require-File $bridge "Compiled KaraokeAsioBridge.exe"
    Require-Directory $BackendDist "Packaged backend directory"
    Build-VirtualAudio

    Copy-Item -LiteralPath $bridge -Destination (Join-Path $BackendDist "KaraokeAsioBridge.exe") -Force
    $sharedLibrary = Join-Path $AsioBuild "KaraokeWasapi.dll"
    Require-File $sharedLibrary "Compiled shared WASAPI library"
    Copy-Item -LiteralPath $sharedLibrary -Destination (Join-Path $BackendDist "KaraokeWasapi.dll") -Force
    Sign-File (Join-Path $BackendDist "KaraokeWasapi.dll")

    Sign-File (Join-Path $BackendDist "KaraokeBackend.exe")
    Sign-File (Join-Path $BackendDist "KaraokeAsioBridge.exe")
}

function Get-TreeSignature([string]$Path) {
    $root = [IO.Path]::GetFullPath($Path).TrimEnd('\')

    return @(
        Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object {
            -not (Test-ExcludedPath $_.FullName `
                @(".cache",".git","__pycache__") `
                @("*.metadata","*.lock","*.tmp","*.part"))
        } |
        ForEach-Object {
            $rel = $_.FullName.Substring($root.Length).TrimStart('\').ToLowerInvariant()
            $sec = [int64]($_.LastWriteTimeUtc.Ticks / 10000000)
            "$rel|$($_.Length)|$sec"
        } |
        Sort-Object
    )
}

function Verify-BackendBase {
    Require-File (Join-Path $BackendDist "KaraokeBackend.exe") "KaraokeBackend.exe"
    Require-File (Join-Path $BackendDist "_internal\ffmpeg.exe") "Bundled FFmpeg"
    Require-File (Join-Path $BackendDist "KaraokeAsioBridge.exe") "KaraokeAsioBridge.exe"
    Require-File (Join-Path $BackendDist "KaraokeWasapi.dll") "KaraokeWasapi.dll"
    Require-File (Join-Path $BackendDist "drivers\ADVoiceVirtualAudio\ADVoiceVirtualAudio.inf") "ADVoiceVirtualAudio.inf"
    Require-File (Join-Path $BackendDist "drivers\ADVoiceVirtualAudio\SimpleAudioSample.sys") "ADVoiceVirtualAudio driver"
    Require-File (Join-Path $BackendDist "drivers\ADVoiceVirtualAudio\SimpleAudioSample.cat") "ADVoiceVirtualAudio catalog"
    Require-Directory (Join-Path $BackendDist "_internal") "PyInstaller internal directory"
    Require-File `
        (Join-Path $BackendDist "_internal\torchfcpe\assets\fcpe_c_v001.pt") `
        "Bundled TorchFCPE checkpoint"
}

function Verify-BackendDist {
    Verify-BackendBase
}

function Build-Frontend {
    Write-Host ""
    Write-Host "[5/7] Building React frontend..."

    Push-Location $Frontend

    try {
        & $script:NpmCmd run build:lighting
        if ($LASTEXITCODE -ne 0) { throw "Keyboard lighting bridge build failed." }
        & $script:NpmCmd run build

        if ($LASTEXITCODE -ne 0) {
            throw "React frontend build failed."
        }
    }
    finally {
        Pop-Location
    }
}

function Verify-Unpacked {
    Require-File (Join-Path $Unpacked $AppExe) "Electron application"
    Require-File (Join-Path $Unpacked "resources\lighting\KeyboardLighting.node") "Keyboard lighting bridge"
    foreach ($LightingSource in @("LICENSE-Wooting.txt", "LICENSE-HIDAPI.txt", "wooting-v1.8.0.zip", "hidapi-d3013f0.zip")) {
        Require-File (Join-Path $Unpacked "resources\lighting\sources\$LightingSource") "Keyboard lighting dependency source/license"
    }
    if (Test-Path -LiteralPath $SceneVideoSource -PathType Leaf) {
        Require-File $PackagedSceneVideo "Karaoke scene video"
    }
    Require-File (Join-Path $PackagedBackend "KaraokeBackend.exe") "Electron backend"
    Require-File (Join-Path $PackagedBackend "KaraokeAsioBridge.exe") "Electron ASIO bridge"
    Require-File (Join-Path $PackagedBackend "KaraokeWasapi.dll") "Electron shared WASAPI library"

    if (Test-Path -LiteralPath (Join-Path $PackagedBackend "_internal\models")) {
        throw "Electron package unexpectedly contains AI models."
    }

    Require-File `
        (Join-Path $PackagedBackend "_internal\engines\msst\inference.py") `
        "Packaged MSST engine"
    Require-File `
        (Join-Path $PackagedBackend "_internal\engines\msst\configs\KimberleyJensen\config_vocals_mel_band_roformer_kj.yaml") `
        "Packaged MSST configuration"
}

function Remove-LegacyEmbeddedAI {
    $modelsPath = Join-Path $BackendDist "_internal\models"
    $msstPath = Join-Path $BackendDist "_internal\engines\msst"

    if (Test-Path -LiteralPath $modelsPath) {
        Write-Host "Removing legacy embedded AI models from backend dist..."
        Remove-Directory $modelsPath
    }

    if (Test-Path -LiteralPath $msstPath) {
        Write-Host "Removing legacy embedded MSST from backend dist..."
        Remove-Directory $msstPath
    }
}

function Add-SmokeTestJunctions {
    $internal = Join-Path $PackagedBackend "_internal"
    $modelsLink = Join-Path $internal "models"
    $engines = Join-Path $internal "engines"
    $msstLink = Join-Path $engines "msst"

    New-Item -ItemType Directory -Path $internal -Force | Out-Null
    New-Item -ItemType Directory -Path $engines -Force | Out-Null

    & cmd.exe /D /C "mklink /J `"$modelsLink`" `"$Models`"" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not create model junction for smoke test." }

    $created = @($modelsLink)
    if (-not (Test-Path -LiteralPath $msstLink -PathType Container)) {
        & cmd.exe /D /C "mklink /J `"$msstLink`" `"$MsstEngine`"" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not create MSST junction for smoke test." }
        $created += $msstLink
    }

    return $created
}

function Remove-SmokeTestJunctions([string[]]$Links) {
    foreach ($link in $Links) {
        if (Test-Path -LiteralPath $link) {
            & cmd.exe /D /C "rmdir `"$link`"" | Out-Null
        }
    }
}

function Build-RuntimeArchive([string]$SourceDirectory) {
    Require-Directory $SourceDirectory "Electron runtime source"

    if (-not $script:TarExe) {
        $script:TarExe = Get-Command tar.exe -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Source -First 1
    }

    if (-not $script:TarExe) {
        throw "Windows tar.exe was not found."
    }

    New-Item -ItemType Directory -Path $PackagesDir -Force | Out-Null

    $tmpArchive = Join-Path $PackagesDir "app-runtime.tmp.zip"
    Remove-Item -LiteralPath $tmpArchive -Force -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Host "Creating compressed application runtime..."
    Write-Host "  CPU policy: compression starts after PyInstaller/Vite/ASIO workers finish."
    Write-Host "  Source:  $SourceDirectory"
    Write-Host "  Archive: $RuntimeArchive"
    Write-Host ""

    $sw = [Diagnostics.Stopwatch]::StartNew()

    # Windows bsdtar selects ZIP compression from the .zip extension.
    & $script:TarExe -a -cf $tmpArchive -C $SourceDirectory .

    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $tmpArchive -PathType Leaf)) {
        Remove-Item -LiteralPath $tmpArchive -Force -ErrorAction SilentlyContinue
        throw "Could not create app-runtime.zip."
    }

    $tmpInfo = Get-Item -LiteralPath $tmpArchive
    if ($tmpInfo.Length -le 0) {
        Remove-Item -LiteralPath $tmpArchive -Force -ErrorAction SilentlyContinue
        throw "Created app-runtime.zip is empty."
    }

    Move-Item -LiteralPath $tmpArchive -Destination $RuntimeArchive -Force

    if (-not (Test-ArchiveIntegrity $RuntimeArchive)) {
        Remove-Item -LiteralPath $RuntimeArchive -Force -ErrorAction SilentlyContinue
        throw "Created app-runtime.zip failed its archive integrity check."
    }

    $sw.Stop()
    Set-PreviousDuration "runtime-archive" $sw.Elapsed.TotalSeconds

    $sourceBytes = [int64]0
    Get-ChildItem -LiteralPath $SourceDirectory -Recurse -File -Force -ErrorAction SilentlyContinue |
        ForEach-Object { $sourceBytes += $_.Length }

    $archiveBytes = (Get-Item -LiteralPath $RuntimeArchive).Length
    $ratio = if ($sourceBytes -gt 0) { 100.0 * $archiveBytes / $sourceBytes } else { 0 }

    Write-Host ("  Runtime source:  {0:N2} GB" -f ($sourceBytes / 1GB))
    Write-Host ("  Runtime archive: {0:N2} GB ({1:N1}% of source)" -f ($archiveBytes / 1GB),$ratio)
    Write-Host ("  Archive elapsed: {0}" -f (Format-Duration $sw.Elapsed.TotalSeconds))
    Write-Host ""

    Require-File $RuntimeArchive "Application runtime archive"
}

function Build-ElectronPackage {
    Write-Host ""
    Write-Host "[5/7] Building complete Electron application..."
    if (-not (Test-Path -LiteralPath $SceneVideoSource -PathType Leaf)) {
        Write-Host "[WARN] Optional karaoke scene video is absent; building without it."
        Write-Host "       $SceneVideoSource"
        Write-Host "       The app already handles this resource as optional."
        Write-Host ""
    }

    Remove-LegacyEmbeddedAI

    $runName = "run-{0}-{1}" -f `
        ([DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")), `
        ([Guid]::NewGuid().ToString("N").Substring(0,8))

    $runRoot = Join-Path $ElectronRoot $runName
    $newUnpacked = Join-Path $runRoot "win-unpacked"

    New-Item -ItemType Directory -Path $runRoot -Force | Out-Null

    Write-Host ""
    Write-Host "Electron output:"
    Write-Host "  $newUnpacked"
    Write-Host ""

    Push-Location $Frontend

    try {
        Write-Host "Building Electron win-unpacked WITHOUT AI models..."
        Write-Host ""

        $outputArg = "--config.directories.output=$runRoot"

        & $script:NpxCmd `
            electron-builder `
            --win `
            --x64 `
            --dir `
            $outputArg

        if ($LASTEXITCODE -ne 0) {
            throw "Electron win-unpacked build failed."
        }
    }
    finally {
        Pop-Location
    }

    if (-not (Test-Path -LiteralPath $newUnpacked -PathType Container)) {
        throw "Electron builder completed but win-unpacked was not created: $newUnpacked"
    }

    if (Test-Path -LiteralPath $SceneVideoSource -PathType Leaf) {
        $mediaDir = Join-Path $newUnpacked "resources\media"
        New-Item -ItemType Directory -Path $mediaDir -Force | Out-Null
        Copy-Item -LiteralPath $SceneVideoSource -Destination (Join-Path $mediaDir "videoplayback.webm") -Force
        Write-Host "Optional karaoke scene video copied into application resources."
    }

    Set-ElectronOutputPath $newUnpacked
    Verify-Unpacked
    Save-ElectronOutputPath $newUnpacked

    Write-Host ""
    Write-Host "Electron package verified successfully."
    Write-Host "Current Electron output:"
    Write-Host "  $Unpacked"
    Write-Host ""

    Remove-OldElectronRuns $Unpacked
}

function Get-ElectronSignFingerprint([string]$ElectronFingerprint) {
    return Get-CombinedFingerprint @(
        $ElectronFingerprint,
        (Get-SigningFingerprint),
        $ElectronSignSchemaVersion
    )
}

function Sign-ElectronPackage {
    Require-File (Join-Path $Unpacked $AppExe) "Electron application"
    Sign-File (Join-Path $Unpacked $AppExe)
}

function Get-ElectronSmokeFingerprint([string]$ElectronFingerprint) {
    return Get-CombinedFingerprint @(
        $ElectronFingerprint,
        (Get-SmallFileFingerprint @($SmokeScript)),
        $ElectronSmokeSchemaVersion
    )
}

function Smoke-TestElectronPackage {
    Verify-Unpacked

    Write-Host ""
    Write-Host "Running packaged backend + AI runtime smoke test..."
    Write-Host ""

    Require-File $SmokeScript "Packaged backend smoke test"
    $links = Add-SmokeTestJunctions

    try {
        & powershell.exe `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $SmokeScript `
            -Executable (Join-Path $PackagedBackend "KaraokeBackend.exe")

        if ($LASTEXITCODE -ne 0) {
            throw "Packaged backend runtime smoke test failed."
        }
    }
    finally {
        Remove-SmokeTestJunctions $links
    }
}

function Find-Inno {
    if ($env:INNO_COMPILER_OVERRIDE -and (Test-Path -LiteralPath $env:INNO_COMPILER_OVERRIDE -PathType Leaf)) {
        return $env:INNO_COMPILER_OVERRIDE
    }

    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 7\ISCC.exe")
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }

    foreach ($key in @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"
    )) {
        try {
            $location = (Get-ItemProperty -LiteralPath $key -ErrorAction Stop).InstallLocation

            if ($location) {
                $candidate = Join-Path $location "ISCC.exe"

                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    return $candidate
                }
            }
        }
        catch {
        }
    }

    return $null
}

function Ensure-RuntimeSource(
    [string]$ElectronFingerprint,
    [string]$FinalizeFingerprint
) {
    $appPath = Join-Path $Unpacked $AppExe

    if (Test-Path -LiteralPath $appPath -PathType Leaf) {
        return
    }

    Write-Host ""
    Write-Host "Runtime archive must be refreshed, but Electron source is missing."
    Write-Host "Rebuilding Electron source only because it is required for runtime packaging."
    Write-Host ""

    Build-ElectronPackage
    Set-State "electron" $ElectronFingerprint
}

function Build-Installer {
    Write-Host ""
    Write-Host "[6/7] Building compact installer..."

    $inno = Find-Inno
    if (-not $inno) { throw "Inno Setup compiler was not found. Install Inno Setup 6." }

    $runName = "run-{0}-{1}" -f ([DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")),([Guid]::NewGuid().ToString("N").Substring(0,8))
    $runDir = Join-Path $InstallerRoot $runName
    New-Item -ItemType Directory -Path $runDir -Force | Out-Null

    Write-Host ""
    Write-Host "Inno Setup:"
    Write-Host "  $inno"
    Write-Host "Installer output:"
    Write-Host "  $runDir"
    Write-Host ""

    Write-Host "Bootstrap-only installer: application runtime stays external."
    Write-Host "AI models are optionally downloaded from pinned public repositories during installation."
    Write-Host "Inno does not recompress Electron/backend/Torch/CUDA."
    Write-Host ""

    & $inno `
        "/DMyAppName=$AppName" `
        "/DMyAppVersion=$AppVersion" `
        "/DMyAppExeName=$AppExe" `
        "/DSetupIcon=$SetupIcon" `
        "/DThemeIconsDir=$ThemeIconsDir" `
        "/DOutputDir=$runDir" `
        $InnoTemplate

    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }

    $candidateExe = Join-Path $runDir ("A&D Voice Setup {0}.exe" -f $AppVersion)
    Require-File $candidateExe "Installer executable"
    Sign-File $candidateExe

    # Bootstrap-only Inno should not create multi-gigabyte .bin payloads.
    $bins = @(Get-ChildItem -LiteralPath $runDir -Filter "*.bin" -File -ErrorAction SilentlyContinue)
    if ($bins.Count -gt 0) {
        $binBytes = ($bins | Measure-Object -Property Length -Sum).Sum
        Write-Host ("  Inno data slices: {0} file(s), {1:N2} MB" -f $bins.Count,($binBytes / 1MB))
        if ($binBytes -gt 100MB) {
            throw "Bootstrap installer unexpectedly contains more than 100 MB of compressed payload."
        }
    }
    else {
        Write-Host "  Inno data slices: none [expected]"
    }

    Save-InstallerOutputPath $runDir
    Remove-OldInstallerRuns $InstallerDir
}

function Create-Checksums {
    Write-Host ""
    Write-Host "Creating SHA-256 checksums..."

    Require-File $ChecksumScript "Checksum generation script"

    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $ChecksumScript `
        -InstallerDirectory $InstallerDir `
        -OutputFile $ChecksumFile

    if ($LASTEXITCODE -ne 0) {
        throw "Could not create SHA-256 checksums."
    }

    Require-File $ChecksumFile "SHA-256 checksum file"
}

function Create-ReleaseSbom {
    Write-Host ""
    Write-Host "Creating mandatory aggregate CycloneDX SBOM..."

    Require-File $Python "Backend Python"
    Require-File $BackendSbomScript "Backend SBOM generator"
    Require-File $ReleaseSbomScript "Aggregate SBOM generator"

    & $Python $BackendSbomScript
    if ($LASTEXITCODE -ne 0) { throw "Backend SBOM generation failed." }

    Push-Location $Frontend
    try {
        & $script:NodeExe "scripts\generate-sbom.mjs" "frontend"
        if ($LASTEXITCODE -ne 0) { throw "Frontend SBOM generation failed." }
    }
    finally { Pop-Location }

    Push-Location (Join-Path $Root "cloudflare")
    try {
        & $script:NodeExe (Join-Path $Frontend "scripts\generate-sbom.mjs") "cloudflare"
        if ($LASTEXITCODE -ne 0) { throw "Cloudflare SBOM generation failed." }
    }
    finally { Pop-Location }

    & $Python $ReleaseSbomScript
    if ($LASTEXITCODE -ne 0) {
        throw "Aggregate SBOM generation failed (unknown licenses block the release)."
    }
    Require-File $GeneratedSbomFile "Generated aggregate SBOM"
    Copy-Item -LiteralPath $GeneratedSbomFile -Destination $SbomFile -Force
    Require-File $SbomFile "Release SBOM artifact"
}

function Create-ReleaseManifest {
    Write-Host ""
    Write-Host "Creating release manifest..."

    Require-File $ManifestScript "Release manifest generation script"

    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $ManifestScript `
        -BackendDir $Backend `
        -FrontendDir $Frontend `
        -InstallerDirectory $InstallerDir `
        -SbomFile $SbomFile `
        -OutputFile $ManifestFile `
        -BuildId $ReleaseBuildId

    if ($LASTEXITCODE -ne 0) {
        throw "Could not create release manifest."
    }

    Require-File $ManifestFile "Release manifest file"
}

function Create-SizeReport {
    Write-Host ""
    Write-Host "Creating size report..."

    Require-File $SizeReportScript "Size report generation script"

    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $SizeReportScript `
        -Directory $Unpacked `
        -OutputFile $SizeReportFile

    if ($LASTEXITCODE -ne 0) {
        throw "Could not create size report."
    }
}

function Test-IsoPayloadExcluded([string]$Path) {
    return Test-ExcludedPath `
        $Path `
        @(".cache",".git","__pycache__") `
        @("*.metadata","*.lock","*.tmp","*.part") `
        @()
}

function Assert-SameVolumeForHardLink(
    [string]$Source,
    [string]$DestinationRoot,
    [string]$Label
) {
    $srcRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Source))
    $dstRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($DestinationRoot))

    if (-not $srcRoot.Equals($dstRoot,[StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label cannot use zero-copy hardlinks because source and ISO view are on different volumes: $srcRoot -> $dstRoot"
    }
}

function New-IsoHardLink(
    [string]$Source,
    [string]$Destination,
    [string]$Label
) {
    Require-File $Source $Label
    Assert-SameVolumeForHardLink $Source $IsoView $Label

    $parent = Split-Path -Parent $Destination
    if ($parent) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Force
    }

    try {
        New-Item -ItemType HardLink -Path $Destination -Value $Source -ErrorAction Stop | Out-Null
    }
    catch {
        throw "Could not create zero-copy hardlink for $Label. Source: $Source Destination: $Destination Error: $($_.Exception.Message)"
    }
}

function Add-TreeToIsoView(
    [string]$SourceRoot,
    [string]$DestinationRoot,
    [string]$Label
) {
    Require-Directory $SourceRoot $Label
    Assert-SameVolumeForHardLink $SourceRoot $IsoView $Label

    $sourceFull = [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\')
    $files = @(
        Get-ChildItem -LiteralPath $sourceFull -Recurse -File -Force -ErrorAction Stop |
        Where-Object { -not (Test-IsoPayloadExcluded $_.FullName) }
    )

    foreach ($file in $files) {
        $relative = $file.FullName.Substring($sourceFull.Length).TrimStart('\')
        $destination = Join-Path $DestinationRoot $relative
        $parent = Split-Path -Parent $destination

        if ($parent) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }

        try {
            New-Item -ItemType HardLink -Path $destination -Value $file.FullName -ErrorAction Stop | Out-Null
        }
        catch {
            throw "Could not create zero-copy hardlink for $Label file: $($file.FullName). Error: $($_.Exception.Message)"
        }
    }

    Write-Host ("  {0}: {1} hardlinks [zero-copy]" -f $Label,$files.Count)
}

function Reset-IsoView {
    if (Test-Path -LiteralPath $IsoView) {
        # iso-view contains hardlinks only. Deleting links does not delete source data.
        Remove-Item -LiteralPath $IsoView -Recurse -Force -ErrorAction Stop
    }

    New-Item -ItemType Directory -Path $IsoView -Force | Out-Null
}

function Build-IsoView {
    Require-File $InstallerExe "Installer executable"
    Require-File $ChecksumFile "SHA-256 checksum file"
    Require-File $SbomFile "Release SBOM artifact"
    Require-File $RuntimeArchive "Application runtime archive"

    Write-Host ""
    Write-Host "Preparing zero-copy ISO view..."
    Write-Host "  No models or runtime data is copied."
    Write-Host "  NTFS hardlinks reference the original files directly."
    Write-Host ""

    Reset-IsoView

    New-IsoHardLink `
        $InstallerExe `
        (Join-Path $IsoView (Split-Path $InstallerExe -Leaf)) `
        "Installer executable"

    New-IsoHardLink `
        $ChecksumFile `
        (Join-Path $IsoView "SHA256SUMS.txt") `
        "SHA-256 checksum file"

    New-IsoHardLink `
        $SbomFile `
        (Join-Path $IsoView "release.cdx.json") `
        "Release SBOM artifact"

    New-IsoHardLink `
        $RuntimeArchive `
        (Join-Path $IsoView "app-runtime.zip") `
        "Application runtime archive"

    $legacyBins = @(
        Get-ChildItem -LiteralPath $IsoView -File -Filter "A&D Voice Setup *.bin" -ErrorAction SilentlyContinue
    )

    if ($legacyBins.Count -gt 0) {
        throw "Legacy Inno .bin files leaked into ISO view."
    }

    if (Test-Path -LiteralPath (Join-Path $IsoView "app") -PathType Container) {
        throw "Duplicated raw Electron app directory exists in ISO view."
    }

    $files = @(
        Get-ChildItem -LiteralPath $IsoView -Recurse -File -Force -ErrorAction Stop
    )

    $bytes = [int64]0
    foreach ($file in $files) {
        $bytes += $file.Length
    }

    Write-Host ""
    Write-Host ("ISO view: {0} files, {1:N2} GB logical, ~0 GB duplicated payload" -f $files.Count,($bytes / 1GB))
    Write-Host ""

    return @{
        Files = $files.Count
        Bytes = $bytes
    }
}

function Create-DistributionIso {
    Write-Host ""
    Write-Host "[7/7] Creating single-file distribution ISO..."
    Write-Host ""

    if (-not $script:Oscdimg) {
        $script:Oscdimg = Find-Oscdimg
    }

    $viewStats = Build-IsoView

    if (Test-Path -LiteralPath $IsoTemp -PathType Leaf) {
        Remove-Item -LiteralPath $IsoTemp -Force
    }

    $volume = ("ADVOICE_" + $AppVersion.Replace(".","_")).ToUpperInvariant()
    if ($volume.Length -gt 32) {
        $volume = $volume.Substring(0,32)
    }

    Write-Host "Creating ISO directly from zero-copy view:"
    Write-Host "  $IsoView"
    Write-Host ("  Payload: {0} files, {1:N2} GB" -f $viewStats.Files,($viewStats.Bytes / 1GB))
    Write-Host "  Runtime: hardlink -> build\\packages\\app-runtime.zip"
    Write-Host "  Models:  optional download during installation or later from application settings"
    Write-Host "  MSST:    bundled inside the application runtime"
    Write-Host ""

    $isoSw = [Diagnostics.Stopwatch]::StartNew()

    New-DistributionIsoImage `
        -SourceDirectory $IsoView `
        -OutputFile $IsoTemp `
        -VolumeName $volume

    $isoSw.Stop()
    Set-PreviousDuration "iso-create" $isoSw.Elapsed.TotalSeconds
    Write-Host ("ISO image creation elapsed: {0}" -f (Format-Duration $isoSw.Elapsed.TotalSeconds))

    Require-File $IsoTemp "Distribution ISO"
    if ((Get-Item -LiteralPath $IsoTemp).Length -le 0) {
        throw "Created ISO is empty."
    }

    New-Item -ItemType Directory -Path $Release -Force | Out-Null

    if (Test-Path -LiteralPath $IsoFile -PathType Leaf) {
        # Explorer may keep the previous release mounted as a virtual CD-ROM.
        # Unmount only this exact release image before replacing it.
        $mountedImage = Get-DiskImage -ImagePath $IsoFile -ErrorAction SilentlyContinue
        if ($mountedImage -and $mountedImage.Attached) {
            Write-Host "Dismounting previous release ISO..."
            Dismount-DiskImage -ImagePath $IsoFile -ErrorAction Stop | Out-Null
        }
        Remove-Item -LiteralPath $IsoFile -Force
    }

    Move-Item -LiteralPath $IsoTemp -Destination $IsoFile -Force
    Require-File $IsoFile "Final distribution ISO"

    Write-Host ""
    Write-Host ("ISO size: {0:N2} GB" -f ((Get-Item -LiteralPath $IsoFile).Length / 1GB))
    Write-Host ""
    Write-Host "Single-file distribution ready:"
    Write-Host "  $IsoFile"
    Write-Host ""
}

function Start-WorkerProcess([string]$Name) {
    $outLog = Join-Path $ParallelDir "$Name.out.log"
    $errLog = Join-Path $ParallelDir "$Name.err.log"
    $exitFile = Join-Path $ParallelDir "$Name.exit"

    Remove-Item -LiteralPath $outLog,$errLog,$exitFile -Force -ErrorAction SilentlyContinue

    $args = @(
        "-NoProfile",
        "-ExecutionPolicy","Bypass",
        "-File",$PSCommandPath,
        "-Mode",$Mode,
        "-Worker",$Name
    )

    return Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList $args `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog `
        -PassThru `
        -NoNewWindow
}

function Run-Parallel([string[]]$Workers) {
    if (Test-Path -LiteralPath $ParallelDir) {
        Remove-Directory $ParallelDir
    }

    New-Item -ItemType Directory -Path $ParallelDir -Force | Out-Null

    $processes = @{}
    $started = @{}
    $expected = @{}
    $lastLog = @{}
    $completedAt = @{}
    $lastProgressSecond = -1

    foreach ($name in $Workers) {
        $started[$name] = [DateTime]::UtcNow
        $expected[$name] = Get-PreviousDuration "worker-$name"
        $processes[$name] = Start-WorkerProcess $name
    }

    while ($true) {
        $allDone = $true
        $now = [DateTime]::UtcNow
        $second = [int](($now - ($started.Values | Sort-Object | Select-Object -First 1)).TotalSeconds)

        foreach ($name in $Workers) {
            $exitFile = Join-Path $ParallelDir "$name.exit"

            if (-not (Test-Path -LiteralPath $exitFile -PathType Leaf)) {
                $allDone = $false
            }
            elseif (-not $completedAt.ContainsKey($name)) {
                $completedAt[$name] = [DateTime]::UtcNow
            }

            $outLog = Join-Path $ParallelDir "$name.out.log"
            $errLog = Join-Path $ParallelDir "$name.err.log"
            $lines = @()

            if (Test-Path -LiteralPath $outLog) {
                $lines += @(Get-Content -LiteralPath $outLog -Tail 2 -ErrorAction SilentlyContinue)
            }

            if (Test-Path -LiteralPath $errLog) {
                $lines += @(Get-Content -LiteralPath $errLog -Tail 2 -ErrorAction SilentlyContinue)
            }

            $lines = @(
                $lines |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
                Select-Object -Last 2
            )

            if ($lines.Count -gt 0) {
                $joined = $lines -join " | "

                if (-not $lastLog.ContainsKey($name) -or $lastLog[$name] -ne $joined) {
                    Write-Host "  $name`: $joined"
                    $lastLog[$name] = $joined
                }
            }

            $proc = $processes[$name]

            if ($proc.HasExited -and -not (Test-Path -LiteralPath $exitFile)) {
                "1" | Set-Content -LiteralPath $exitFile -Encoding ASCII
            }
        }

        if ($allDone) {
            break
        }

        if ($second -ne $lastProgressSecond -and ($second % 10) -eq 0) {
            Write-Host ""
            foreach ($name in $Workers) {
                $exitFile = Join-Path $ParallelDir "$name.exit"
                if ($completedAt.ContainsKey($name)) {
                    $elapsed = ($completedAt[$name] - $started[$name]).TotalSeconds
                }
                else {
                    $elapsed = ([DateTime]::UtcNow - $started[$name]).TotalSeconds
                }

                if (Test-Path -LiteralPath $exitFile -PathType Leaf) {
                    $raw = Get-Content -LiteralPath $exitFile -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($raw -eq "0") {
                        Write-Host ("  [DONE] {0} | elapsed {1}" -f $name,(Format-Duration $elapsed))
                    }
                    else {
                        Write-Host ("  [FAILED] {0} | elapsed {1}" -f $name,(Format-Duration $elapsed))
                    }
                }
                else {
                    Write-StepProgress $name $elapsed $expected[$name]
                }
            }
            Write-Host ""
            $lastProgressSecond = $second
        }

        Start-Sleep -Seconds 2
    }

    $failed = $false

    foreach ($name in $Workers) {
        $proc = $processes[$name]
        try { $proc.WaitForExit() } catch {}

        if ($completedAt.ContainsKey($name)) {
            $elapsed = ($completedAt[$name] - $started[$name]).TotalSeconds
        }
        else {
            $elapsed = ([DateTime]::UtcNow - $started[$name]).TotalSeconds
        }

        $exitFile = Join-Path $ParallelDir "$name.exit"
        $code = 1

        if (Test-Path -LiteralPath $exitFile) {
            $raw = Get-Content -LiteralPath $exitFile -ErrorAction SilentlyContinue | Select-Object -First 1

            if ($raw -match '^[0-9]+$') {
                $code = [int]$raw
            }
        }

        if ($code -ne 0) {
            $failed = $true
            Write-Host ""
            Write-Host "[ERROR] Parallel worker failed: $name"
            Write-Host "Exit code: $code"

            $outLog = Join-Path $ParallelDir "$name.out.log"
            $errLog = Join-Path $ParallelDir "$name.err.log"

            if (Test-Path -LiteralPath $outLog) {
                Get-Content -LiteralPath $outLog
            }

            if (Test-Path -LiteralPath $errLog) {
                Get-Content -LiteralPath $errLog
            }
        }
        else {
            Set-PreviousDuration "worker-$name" $elapsed
            Write-Host ("[OK] Parallel worker: {0} | elapsed {1}" -f $name,(Format-Duration $elapsed))
        }
    }

    if ($failed) {
        throw "Parallel build stage failed."
    }
}

function Parallel-FullBuild {
    Write-Header "SMART PARALLEL BUILD"
    Write-Host "Parallel policy: backend + ASIO + frontend + AI verification"
    Write-Host "Large file sync: robocopy /MT:32 /J"
    Write-Host "Runtime compression is intentionally deferred to avoid CPU/RAM contention."
    Write-Host ""

    $force = ($Mode -eq "clean")

    $script:BackendChanged = Test-StepNeeded `
        "backend" `
        $script:BackendFingerprint `
        @(
            (Join-Path $BackendDist "KaraokeBackend.exe")
        ) `
        -Force:$force

    $script:AsioChanged = Test-StepNeeded `
        "asio" `
        $script:AsioFingerprint `
        @((Join-Path $AsioBuild "KaraokeAsioBridge.exe"),(Join-Path $AsioBuild "KaraokeWasapi.dll")) `
        -Force:$force

    $script:FrontendChanged = Test-StepNeeded `
        "frontend" `
        $script:FrontendFingerprint `
        @(
            (Join-Path $Build "frontend\dist\index.html"),
            (Join-Path $Build "lighting\KeyboardLighting.node"),
            (Join-Path $Build "lighting\sources\LICENSE-Wooting.txt"),
            (Join-Path $Build "lighting\sources\LICENSE-HIDAPI.txt"),
            (Join-Path $Build "lighting\sources\wooting-v1.8.0.zip"),
            (Join-Path $Build "lighting\sources\hidapi-d3013f0.zip")
        ) `
        -Force:$force

    $script:ModelsChanged = Test-StepNeeded `
        "models" `
        $script:ModelsFingerprint `
        @((Join-Path $MsstEngine "inference.py")) `
        -Force:$force

    $workers = @()
    if ($script:BackendChanged) { $workers += "backend" }
    if ($script:AsioChanged) { $workers += "asio" }
    if ($script:FrontendChanged) { $workers += "frontend" }
    if ($script:ModelsChanged) { $workers += "models" }

    if ($workers.Count -eq 0) {
        Write-Host ""
        Write-Host "Backend, ASIO, frontend and AI models are unchanged. Nothing to rebuild."
        return
    }

    Write-Host ""
    Write-Host "Running changed steps simultaneously:"
    foreach ($worker in $workers) { Write-Host "  - $worker" }
    Write-Host ""

    Run-Parallel $workers

    if ($script:BackendChanged) { Set-State "backend" $script:BackendFingerprint }
    if ($script:AsioChanged) { Set-State "asio" $script:AsioFingerprint }
    if ($script:FrontendChanged) { Set-State "frontend" $script:FrontendFingerprint }
    if ($script:ModelsChanged) { Set-State "models" $script:ModelsFingerprint }

    Write-Host ""
    Write-Host "Smart parallel build completed."
}

function Parallel-FastBuild {
    Write-Host ""
    Write-Host "Fast mode now uses the same smart incremental pipeline."
    Parallel-FullBuild
}

function Initialize-WorkerCommands {
    $script:NodeExe = (Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    $script:NpmCmd = (Get-Command npm.cmd -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    $script:NpxCmd = (Get-Command npx.cmd -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    $script:Ffmpeg = (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
}

try {
    if ($Worker) {
        Initialize-WorkerCommands
        New-Item -ItemType Directory -Path $ParallelDir -Force | Out-Null

        $workerExitFile = Join-Path $ParallelDir "$Worker.exit"
        $workerCode = 0

        try {
            switch ($Worker) {
                "backend" {
                    Build-Backend
                }
                "asio" {
                    Build-Asio
                }
                "frontend" {
                    Build-Frontend
                }
                "models" {
                    Check-Models
                }
            }
        }
        catch {
            $workerCode = 1
            Write-Host ""
            Write-Host ("=" * 60)
            Write-Host " BUILD FAILED"
            Write-Host ("=" * 60)
            Write-Host ""
            Write-Host "[ERROR] $($_.Exception.Message)"
            Write-Host ""

            if ($_.ScriptStackTrace) {
                Write-Host "PowerShell stack:"
                Write-Host $_.ScriptStackTrace
                Write-Host ""
            }
        }
        finally {
            $workerCode.ToString() | Set-Content -LiteralPath $workerExitFile -Encoding ASCII
        }

        exit $workerCode
    }

    Write-Header "A&D VOICE - COMPACT INSTALLER"

    Write-Host "Build mode:"
    Write-Host "  $Mode"
    Write-Host ""
    Write-Host "Project:"
    Write-Host "  $Root"
    Write-Host ""
    Write-Host "Build intermediates:"
    Write-Host "  $Build"
    Write-Host ""
    Write-Host "Downloaded resources:"
    Write-Host "  $Downloads"
    Write-Host ""
    Write-Host "Final release:"
    Write-Host "  $Release"
    Write-Host ""

    $overall = [Diagnostics.Stopwatch]::StartNew()

    if ($Mode -ne "setup") {
        Stop-BuildProcesses
    } else {
        Write-Host "Setup-only mode: the running application can stay open."
    }
    Check-Environment
    # Installer-only builds still calculate component fingerprints to reuse the
    # existing cache, so optional tool paths must be initialized in every mode.
    Initialize-WorkerCommands
    Prepare-Output

    if ($Mode -eq "setup") {
        $legacyInstallerFp = Get-LegacyV23InstallerFingerprint
        $installerFp = Get-InstallerFingerprint

        [void](Migrate-StateIfCompatible `
            "installer" `
            $installerFp `
            @($legacyInstallerFp) `
            @($InstallerExe,$ChecksumFile,$ManifestFile,$SizeReportFile,$SbomFile))

        $setupNeeded = Test-StepNeeded `
            "installer" `
            $installerFp `
            @($InstallerExe,$ChecksumFile,$ManifestFile,$SizeReportFile,$SbomFile)

        if ($setupNeeded) {
            Write-StepEstimate "installer"
            $sw = [Diagnostics.Stopwatch]::StartNew()
            Build-Installer
            Create-ReleaseSbom
            Create-Checksums
            Create-ReleaseManifest
            Create-SizeReport
            $sw.Stop()
            Set-PreviousDuration "installer" $sw.Elapsed.TotalSeconds
            Set-State "installer" $installerFp
        }

        $overall.Stop()
        Write-Host ""
        Write-Host ("Setup-only build elapsed: {0}" -f (Format-Duration $overall.Elapsed.TotalSeconds))
        Write-Header "SETUP BUILD COMPLETED SUCCESSFULLY"
        Write-Host "Installer:"
        Write-Host "  $InstallerExe"
        Write-Host ""
        exit 0
    }

    # Fast dependency graph. Builder source changes by themselves do NOT
    # invalidate application artifacts. Only component inputs/schemas do.
    $legacyBackendFp = Get-BackendInputFingerprint
    $legacyAsioFp = Get-AsioInputFingerprint
    $legacyFrontendFp = Get-FrontendInputFingerprint
    $legacyModelsFp = Get-ModelsInputFingerprint

    $script:BackendFingerprint = Get-CombinedFingerprint @(
        $legacyBackendFp,
        $BackendSchemaVersion
    )
    $script:AsioFingerprint = Get-CombinedFingerprint @(
        $legacyAsioFp,
        $AsioSchemaVersion
    )
    $script:FrontendFingerprint = Get-CombinedFingerprint @(
        $legacyFrontendFp,
        $FrontendSchemaVersion
    )
    $script:ModelsFingerprint = Get-CombinedFingerprint @(
        $legacyModelsFp,
        $ModelsSchemaVersion
    )

    # Zero-cost state migration from v23: if the old hash still exactly matches
    # current inputs and the artifact exists, rewrite only the hash and skip build.
    [void](Migrate-StateIfCompatible `
        "backend" `
        $script:BackendFingerprint `
        @($legacyBackendFp) `
        @(
            (Join-Path $BackendDist "KaraokeBackend.exe")
        ))

    [void](Migrate-StateIfCompatible `
        "asio" `
        $script:AsioFingerprint `
        @($legacyAsioFp) `
        @((Join-Path $AsioBuild "KaraokeAsioBridge.exe"),(Join-Path $AsioBuild "KaraokeWasapi.dll")))

    [void](Migrate-StateIfCompatible `
        "frontend" `
        $script:FrontendFingerprint `
        @($legacyFrontendFp) `
        @((Join-Path $Build "frontend\dist\index.html")))

    [void](Migrate-StateIfCompatible `
        "models" `
        $script:ModelsFingerprint `
        @($legacyModelsFp) `
        @((Join-Path $MsstEngine "inference.py")))

    $legacyFinalizeFp = Get-LegacyV23FinalizeFingerprint `
        $legacyBackendFp `
        $legacyAsioFp

    $finalizeFp = Get-FinalizeFingerprint

    [void](Migrate-StateIfCompatible `
        "finalize" `
        $finalizeFp `
        @($legacyFinalizeFp) `
        @((Join-Path $BackendDist "KaraokeAsioBridge.exe"),(Join-Path $BackendDist "KaraokeWasapi.dll")))

    $legacyElectronFp = Get-LegacyV23ElectronFingerprint `
        $legacyBackendFp `
        $legacyAsioFp `
        $legacyFrontendFp `
        $legacyFinalizeFp

    $electronFp = Get-CombinedFingerprint @(
        (Get-ElectronFingerprint),
        $finalizeFp,
        $ReleaseBuildId
    )

    # v23 considered app-runtime.zip the Electron output. Accept it during
    # migration so upgrading the builder alone does not rebuild Electron.
    [void](Migrate-StateIfCompatible `
        "electron" `
        $electronFp `
        @($legacyElectronFp) `
        @($RuntimeArchive))

    $electronSignFp = Get-ElectronSignFingerprint $electronFp
    $runtimeFp = Get-RuntimeFingerprint $electronFp $electronSignFp

    # v23 had no separate runtime state. A valid v23 Electron state + archive
    # proves this exact runtime archive already exists, so seed runtime state.
    if ([string]::IsNullOrWhiteSpace((Get-State "runtime")) -and
        (Test-Path -LiteralPath $RuntimeArchive -PathType Leaf)) {
        $savedElectron = Get-State "electron"

        if ($savedElectron -eq $electronFp) {
            Set-State "runtime" $runtimeFp
            Write-Host "  runtime: state initialized from compatible archive [skip]"
        }
    }

    $legacyInstallerFp = Get-LegacyV23InstallerFingerprint
    $installerFp = Get-InstallerFingerprint

    [void](Migrate-StateIfCompatible `
        "installer" `
        $installerFp `
        @($legacyInstallerFp) `
        @($InstallerExe,$ChecksumFile,$ManifestFile,$SizeReportFile,$SbomFile))

    $legacyIsoFp = Get-LegacyV23IsoFingerprint `
        $legacyInstallerFp `
        $legacyElectronFp `
        $legacyModelsFp

    $isoFp = Get-IsoFingerprint `
        $installerFp `
        $runtimeFp

    [void](Migrate-StateIfCompatible `
        "iso" `
        $isoFp `
        @($legacyIsoFp) `
        @($IsoFile))

    if ($Mode -in @("installer","setup")) {
        $script:BackendChanged = $false
        $script:AsioChanged = $false
        $script:FrontendChanged = $false
        $script:ModelsChanged = $false
    }
    else {
        Parallel-FullBuild
    }

    if ($Mode -eq "installer") {
        $modelsNeeded = Test-StepNeeded `
            "models" `
            $script:ModelsFingerprint `
            @((Join-Path $MsstEngine "inference.py")) `
            -Force:($Mode -eq "clean")

        if ($modelsNeeded) {
            Check-Models
            Set-State "models" $script:ModelsFingerprint
        }
        else {
            Write-Host "  AI model verification: unchanged [skip]"
        }
    }
    elseif (-not $script:ModelsChanged) {
        Write-Host "  AI model verification: unchanged [skip]"
    }

    if ($Mode -notin @("installer","setup")) {
        $needFinalize = Test-StepNeeded `
            "finalize" `
            $finalizeFp `
            @((Join-Path $BackendDist "KaraokeAsioBridge.exe"),(Join-Path $BackendDist "KaraokeWasapi.dll")) `
            -Force:($Mode -eq "clean")

        if ($needFinalize) {
            Finalize-Asio
            Set-State "finalize" $finalizeFp

            # Finalized backend bytes changed, therefore Electron input changes.
            $electronFp = Get-CombinedFingerprint @(
                (Get-ElectronFingerprint),
                $finalizeFp,
                $ReleaseBuildId
            )
            $electronSignFp = Get-ElectronSignFingerprint $electronFp
    $runtimeFp = Get-RuntimeFingerprint $electronFp $electronSignFp
            $isoFp = Get-IsoFingerprint `
                $installerFp `
                $runtimeFp
        }
        else {
            Write-Host "  backend signing / ASIO finalize: unchanged [skip]"
        }
    }

    Write-BackendBuildIdentity

    # Electron is rebuilt only when its real inputs changed. A valid runtime
    # archive is enough to preserve a no-op build even if an old win-unpacked
    # directory was cleaned.
    $electronRequiredOutput = if (Test-Path -LiteralPath $RuntimeArchive -PathType Leaf) {
        $RuntimeArchive
    }
    else {
        Join-Path $Unpacked $AppExe
    }

    $electronNeeded = Test-StepNeeded `
        "electron" `
        $electronFp `
        @($electronRequiredOutput) `
        -Force:($Mode -eq "clean")

    if ($electronNeeded) {
        Write-StepEstimate "electron"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        Build-ElectronPackage
        $sw.Stop()
        Set-PreviousDuration "electron" $sw.Elapsed.TotalSeconds
        Set-State "electron" $electronFp
    }

    # Electron signing is cached separately.
    $electronSignFp = Get-ElectronSignFingerprint $electronFp
    $electronSignNeeded = Test-StepNeeded `
        "electron-sign" `
        $electronSignFp `
        @((Join-Path $Unpacked $AppExe)) `
        -Force:($Mode -eq "clean")

    if ($electronSignNeeded) {
        Sign-ElectronPackage
        Set-State "electron-sign" $electronSignFp
    }

    # Smoke-test is also cached separately; unchanged Electron never starts backend.
    $electronSmokeFp = Get-ElectronSmokeFingerprint $electronFp
    $electronSmokeNeeded = Test-StepNeeded `
        "electron-smoke" `
        $electronSmokeFp `
        @((Join-Path $Unpacked $AppExe)) `
        -Force:($Mode -eq "clean")

    if ($electronSmokeNeeded) {
        if ($SkipPackageSmoke) {
            Write-Warning "Packaged backend smoke test skipped by -SkipPackageSmoke."
        }
        else {
            Smoke-TestElectronPackage
            Set-State "electron-smoke" $electronSmokeFp
        }
    }

    # Runtime archive is independently cached. Changing runtime compression
    # rules rebuilds ONLY the archive + ISO, never Electron.
    $electronSignFp = Get-ElectronSignFingerprint $electronFp
    $runtimeFp = Get-RuntimeFingerprint $electronFp $electronSignFp
    $runtimeNeeded = Test-StepNeeded `
        "runtime" `
        $runtimeFp `
        @($RuntimeArchive) `
        -Force:($Mode -eq "clean")

    if ($runtimeNeeded) {
        Ensure-RuntimeSource $electronFp $finalizeFp

        Write-StepEstimate "runtime-archive"
        Build-RuntimeArchive $Unpacked
        Set-State "runtime" $runtimeFp
    }

    $installerFp = Get-InstallerFingerprint
    $installerNeeded = Test-StepNeeded `
        "installer" `
        $installerFp `
        @($InstallerExe,$ChecksumFile,$ManifestFile,$SizeReportFile,$SbomFile) `
        -Force:($Mode -eq "clean")

    if ($installerNeeded) {
        Write-StepEstimate "installer"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        Build-Installer
        Create-ReleaseSbom
        Create-Checksums
        Create-ReleaseManifest
        Create-SizeReport
        $sw.Stop()
        Set-PreviousDuration "installer" $sw.Elapsed.TotalSeconds
        Set-State "installer" $installerFp
    }

    $isoFp = Get-IsoFingerprint `
        $installerFp `
        $runtimeFp

    $isoNeeded = Test-StepNeeded `
        "iso" `
        $isoFp `
        @($IsoFile) `
        -Force:($Mode -eq "clean")

    if ($isoNeeded) {
        Write-StepEstimate "iso"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        Create-DistributionIso
        $sw.Stop()
        Set-PreviousDuration "iso" $sw.Elapsed.TotalSeconds
        Set-State "iso" $isoFp
    }

    $overall.Stop()
    Write-Host ""
    Write-Host ("Smart build elapsed: {0}" -f (Format-Duration $overall.Elapsed.TotalSeconds))

    Remove-Directory $TempDir

    Write-Header "BUILD COMPLETED SUCCESSFULLY"

    Write-Host "Single-file compact distribution:"
    Write-Host "  $IsoFile"
    Write-Host ""
    Write-Host "The ISO is built from a zero-copy hardlink view; AI weights stay outside the runtime."
    Write-Host "Only artifacts whose real inputs changed are rebuilt."
    Write-Host ""

    Start-Process explorer.exe $Release
    exit 0
}
catch {
    Write-Host ""
    Write-Host ("=" * 60)
    Write-Host " BUILD FAILED"
    Write-Host ("=" * 60)
    Write-Host ""
    Write-Host "[ERROR] $($_.Exception.Message)"
    Write-Host ""

    if ($_.ScriptStackTrace) {
        Write-Host "PowerShell stack:"
        Write-Host $_.ScriptStackTrace
        Write-Host ""
    }

    exit 1
}
