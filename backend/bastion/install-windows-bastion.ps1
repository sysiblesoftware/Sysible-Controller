#Requires -Version 5.1
<#
.SYNOPSIS
    Set up a Windows host as a Sysible Relay bastion (jump box) -- guided.

.DESCRIPTION
    The Sysible Relay is NOT an agent. Nothing from Sysible runs on the bastion.
    The bastion is a pure SSH jump host: the Controller opens an SSH ProxyJump to
    it and the bastion forwards that TCP connection on to an internal target host.

    Run this on the BASTION. With no arguments it is fully interactive: it asks for
    the Sysible Controller's address and a one-time relay enrollment token (from the
    console: Host Enrollment -> Relay Enrollment -> "Enroll a bastion"), then sets itself up:

      * BOOTSTRAP mode (bastion can reach the Controller): fetches the relay PUBLIC
        key from the Controller over HTTPS, registers this bastion into the console's
        "Relay hosts" panel, and configures itself.
      * OFFLINE mode (segmented network): if the Controller can't be reached, it
        falls back to pasting the relay public key by hand; no phone-home.

    Either way it, idempotently:
      1. Ensures OpenSSH Server is installed, running, auto-starting, firewalled.
      2. Creates a dedicated NON-admin 'sysible-relay' account (strong random
         password, never logs in interactively).
      3. Installs the Controller's public key with strict SYSTEM/Administrators ACLs
         (so you never touch administrators_authorized_keys).
      4. Appends a hardened `Match User` block: key-only auth, forwarding for this
         account ONLY, restricted to the permitted targets, no shell/TTY/agent/X11.
      5. Validates (`sshd -t`), restarts sshd, prints the verify command.

.PARAMETER Controller
    Base URL or host of the Sysible Controller, e.g. https://ctrl.example:9000 or
    just 192.168.1.10 (assumed https on :9000). Used to fetch the relay key and
    self-register. Prompted if omitted.

.PARAMETER Token
    One-time relay enrollment token from the console. Prompted if omitted (leave
    blank to go OFFLINE and paste the key instead).

.PARAMETER ControllerCert
    Optional path to the Controller's TLS certificate (its certs/server.crt) to pin
    the HTTPS connection. If omitted and the cert is self-signed, you'll be shown
    the fingerprint and asked to confirm (trust-on-first-use).

.PARAMETER PublicKey / -PublicKeyPath
    Offline mode: supply the relay PUBLIC key directly instead of bootstrapping.

.PARAMETER PermitOpen
    Internal targets the relay account may forward to, each 'host:port'
    (e.g. 192.168.1.50:22). In bootstrap mode the Controller may suggest these;
    anything you pass here is merged in. 'any' allows all (not recommended).

.PARAMETER User
    Relay account name. Default: 'sysible-relay'.

.PARAMETER Port
    SSH port. Default: 22.

.EXAMPLE
    # Fully guided
    .\install-windows-bastion.ps1

.EXAMPLE
    # Non-interactive bootstrap
    .\install-windows-bastion.ps1 -Controller https://192.168.1.10:9000 `
        -Token 8f3c...e1 -PermitOpen 192.168.1.50:22,192.168.1.51:22

.EXAMPLE
    # Offline (no phone-home)
    .\install-windows-bastion.ps1 -PublicKeyPath C:\Temp\relay_ed25519.pub `
        -PermitOpen 192.168.1.50:22

.NOTES
    Run in an ELEVATED PowerShell. Windows 10 / Server 2019+ with OpenSSH Server.
#>

[CmdletBinding()]
param(
    [string]   $Controller,
    [string]   $Token,
    [string]   $ControllerCert,
    [string]   $PublicKey,
    [string]   $PublicKeyPath,
    [string[]] $PermitOpen = @(),
    [ValidatePattern('^[A-Za-z0-9._-]{1,32}$')]
    [string]   $User = 'sysible-relay',
    [ValidateRange(1, 65535)]
    [int]      $Port = 22
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ------------------------------------------------------------------ helpers ---
function Write-Step  { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok    { param([string]$m) Write-Host "    [ok] $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "    [!]  $m" -ForegroundColor Yellow }

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script must be run from an ELEVATED PowerShell (Run as Administrator).'
    }
}

function New-StrongPassword {
    # 24 chars from a 66-char alphabet, REJECTION-SAMPLED so there is no modulo bias
    # (256 % 66 != 0 would otherwise skew toward the first 58 characters). Uses raw
    # CSPRNG bytes (no GetInt32) so it runs on any .NET Framework version.
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!@#%^*-_=+'
    $rng   = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $limit = 256 - (256 % $chars.Length)   # largest whole multiple of the alphabet
    $one   = New-Object 'System.Byte[]' 1
    $sb    = New-Object System.Text.StringBuilder
    while ($sb.Length -lt 24) {
        $rng.GetBytes($one)
        if ($one[0] -lt $limit) { [void]$sb.Append($chars[$one[0] % $chars.Length]) }
    }
    $sb.ToString()
}

function Get-Sha256Hex {
    param([byte[]]$Bytes)
    # SHA-256 of raw cert DER, computed manually so it works on any .NET Framework
    # version (avoids the SHA-1 X509 Thumbprint and the .NET-4.8-only hash overload).
    $h = [System.Security.Cryptography.SHA256]::Create().ComputeHash($Bytes)
    ($h | ForEach-Object { $_.ToString('x2') }) -join ''
}

function Test-RelayUserName {
    # Re-validate a username that did NOT come through parameter binding (e.g. one the
    # Controller returned in the bootstrap response). ValidatePattern only runs at
    # bind time, so a server-supplied value would otherwise be injected raw into
    # sshd_config / used as the account. Reject bad charsets and privileged names.
    param([string]$Name)
    if ($Name -notmatch '^[A-Za-z0-9._-]{1,32}$') {
        throw "Controller returned an invalid relay user name: '$Name'"
    }
    if ('administrator','administrators','system','guest' -contains $Name.ToLower()) {
        throw "Controller returned a privileged relay user name ('$Name') -- refusing."
    }
    return $Name
}

function Test-PermitOpenTarget {
    # Validate a single sshd PermitOpen target. OpenSSH PermitOpen is per host:port ONLY:
    # it is NOT CIDR-aware (so '10.20.0.0/16:22' would silently match nothing -- forwarding
    # fails closed while the operator thinks the subnet is allowed) and takes exactly one
    # host and one port. Mirror the Linux validator's charset ('^[A-Za-z0-9._-]+:[0-9]{1,5}$'
    # in install-linux-bastion.sh) -- no '/', no extra ':' -- AND bound the port to 1..65535
    # (the {1,5} digit run alone would accept e.g. host:99999).
    param([string]$Target)
    if ($Target -notmatch '^[A-Za-z0-9._-]+:[0-9]{1,5}$') { return $false }
    $port = [int]($Target.Split(':')[-1])
    if ($port -lt 1 -or $port -gt 65535) { return $false }
    return $true
}

function Test-PublicKeyLine {
    param([string]$key)
    $key = ($key | Out-String).Trim()
    if (-not $key) { throw 'No public key content was provided.' }
    if ($key -match "[`r`n]") { throw 'Public key must be a single line.' }
    if ($key -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp\d+@openssh.com)\s') {
        throw "That does not look like an OpenSSH PUBLIC key line (did you paste a private key?).`n  Got: $($key.Substring(0, [Math]::Min(40, $key.Length)))..."
    }
    return $key
}

function Read-PublicKeyFromFile {
    param([string]$path)
    if (-not (Test-Path -LiteralPath $path)) { throw "Public key file not found: $path" }
    $line = Get-Content -LiteralPath $path |
        Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') } |
        Select-Object -First 1
    return Test-PublicKeyLine $line
}

function Resolve-ControllerUrl {
    param([string]$raw)
    $raw = $raw.Trim()
    if (-not $raw) { return $null }
    if ($raw -notmatch '^https?://') { $raw = "https://$raw" }
    $u = [Uri]$raw
    if (-not $u.IsDefaultPort -and $u.Port -gt 0) { return $u.GetLeftPart([UriPartial]::Authority) }
    # No explicit port -> default to the Controller's 9000.
    if ($raw -match ':\d+') { return $u.GetLeftPart([UriPartial]::Authority) }
    return "$($u.Scheme)://$($u.Host):9000"
}

# Ordered, de-duplicated list of Controller base URLs to try: the operator-given one
# first, then the SAME host on the agent-API port (9000, where the bootstrap endpoint
# lives natively) and the console port (8800, via the BFF). A segmented bastion often
# reaches one but not the other, so we try them automatically instead of making the
# operator guess which port is open across their network segments.
function Get-BootstrapCandidates {
    param([string]$baseUrl)
    $u = [Uri]$baseUrl
    $ctrlHost = $u.Host
    $scheme = $u.Scheme
    $out = New-Object System.Collections.Generic.List[string]
    $out.Add($u.GetLeftPart([UriPartial]::Authority))
    foreach ($p in 9000, 8800) {
        $cand = ('{0}://{1}:{2}' -f $scheme, $ctrlHost, $p)
        if (-not $out.Contains($cand)) { $out.Add($cand) }
    }
    return $out
}

# Capture the leaf TLS cert from host:port with a RAW SOCKET. This is the reliable way on
# Windows PowerShell 5.1 -- Invoke-WebRequest's ServerCertificateValidationCallback capture
# is flaky and can report "no TLS response" even when the port is plainly reachable (e.g.
# curl.exe to the same port works). Returns an X509Certificate2, or $null if unreachable.
function Get-LeafCert {
    param([string]$ctrlHost, [int]$port, [int]$timeoutMs = 8000)
    $tcp = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $tcp.BeginConnect($ctrlHost, $port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($timeoutMs)) { return $null }
        $tcp.EndConnect($iar)
        $cb  = [System.Net.Security.RemoteCertificateValidationCallback] { param($a, $b, $c, $d) $true }
        $ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, $cb)
        try {
            $ssl.AuthenticateAsClient($ctrlHost, $null,
                [System.Security.Authentication.SslProtocols]::Tls12, $false)
            if (-not $ssl.RemoteCertificate) { return $null }
            return [System.Security.Cryptography.X509Certificates.X509Certificate2]$ssl.RemoteCertificate
        } finally { $ssl.Dispose() }
    } catch { return $null }
    finally { $tcp.Close() }
}

# Write a cert to a temp PEM file for curl --cacert pinning; returns the path.
function Write-CertPem {
    param([System.Security.Cryptography.X509Certificates.X509Certificate2]$cert)
    $b64  = [Convert]::ToBase64String($cert.RawData, 'InsertLineBreaks')
    $pem  = "-----BEGIN CERTIFICATE-----`r`n$b64`r`n-----END CERTIFICATE-----`r`n"
    $path = [System.IO.Path]::GetTempFileName()
    Set-Content -LiteralPath $path -Value $pem -Encoding ascii
    return $path
}

# Bootstrap: POST the token to the Controller -- capturing the cert with a raw socket and
# sending via curl.exe pinned to that exact cert (--cacert, never -k). curl.exe is the same
# transport that downloaded this script, so it works where Invoke-WebRequest didn't.
function Invoke-Bootstrap {
    param([string]$baseUrl, [string]$token, [string]$certPath)

    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
        throw "curl.exe not found -- bootstrap needs it (Windows 10 1803+/Server 2019+). Use OFFLINE mode instead."
    }

    # STEP 0 -- find a REACHABLE Controller port among the candidates (given -> :9000 ->
    # :8800). Fixes the old "could not retrieve the certificate" dead-end: if the given port
    # is blocked across the bastion's subnet we try the others before giving up.
    $candidates = Get-BootstrapCandidates $baseUrl
    $working = $null; $capCert = $null
    foreach ($cand in $candidates) {
        Write-Step "Trying Controller at $cand ..."
        $u = [Uri]$cand
        $c = Get-LeafCert $u.Host $u.Port
        if ($c) { $working = $cand; $capCert = $c; break }
        Write-Warn2 "No TLS response from $cand -- trying the next port."
    }
    if (-not $working) {
        throw ("Could not reach the Controller on any of: " + ($candidates -join ', ') +
               ". This bastion has no network path to the Controller on those ports -- check " +
               "routing/firewall between this host's subnet and the Controller (or open one of " +
               "9000/8800). Falling back to OFFLINE, which needs no inbound-to-Controller path.")
    }
    $baseUrl  = $working
    $endpoint = "$baseUrl/api/relay/bootstrap"
    Write-Ok "Controller reachable at $baseUrl"

    $cleanup = @()
    try {
        # STEP 1 -- decide the PINNED cert. -ControllerCert pins that file; else TOFU: show
        # the captured leaf SHA-256 and pin it only after the operator confirms out of band.
        if ($certPath) {
            if (-not (Test-Path -LiteralPath $certPath)) { throw "Controller cert not found: $certPath" }
            $pemFile = $certPath
        } else {
            Write-Warn2 "Controller TLS certificate SHA-256:"
            Write-Host  "        $(Get-Sha256Hex $capCert.RawData)" -ForegroundColor Yellow
            $ans = Read-Host "Compare this with the Controller (out of band) and trust it? [y/N]"
            if ($ans -notmatch '^(y|yes)$') { throw 'Controller certificate not trusted; aborting bootstrap.' }
            $pemFile = Write-CertPem $capCert; $cleanup += $pemFile
        }

        # STEP 2 -- POST the token with curl.exe, pinned to the confirmed cert. A MITM that
        # showed one cert can't answer the real request with another.
        $body = @{ token = $token; hostname = [System.Net.Dns]::GetHostName(); os = 'windows' } | ConvertTo-Json -Compress
        $bodyFile = [System.IO.Path]::GetTempFileName(); $cleanup += $bodyFile
        Set-Content -LiteralPath $bodyFile -Value $body -Encoding ascii -NoNewline
        Write-Step "Contacting Controller at $endpoint"
        $raw = & curl.exe -sS --fail --max-time 20 --cacert $pemFile `
            -H 'Content-Type: application/json' -X POST --data-binary ("@" + $bodyFile) $endpoint 2>&1
        if ($LASTEXITCODE -ne 0) { throw ("bootstrap request failed: " + ($raw -join ' ')) }
        return ($raw | Out-String | ConvertFrom-Json)
    } finally {
        foreach ($f in $cleanup) { Remove-Item -LiteralPath $f -ErrorAction SilentlyContinue }
    }
}

# --------------------------------------------------------------- run ----------
Assert-Admin

Write-Host ''
Write-Host 'Sysible Relay -- Windows bastion setup' -ForegroundColor White
Write-Host '(This host becomes a jump box. Nothing from Sysible runs here permanently.)'
Write-Host ''

$keyLine   = $null
$suggested = @()
$script:AgentTunnelKey = $null   # dedicated key for agents BEHIND this bastion (bootstrap only)

# Decide mode: explicit offline key wins; else try bootstrap; else prompt.
if ($PublicKey)     { $keyLine = Test-PublicKeyLine $PublicKey }
elseif ($PublicKeyPath) { $keyLine = Read-PublicKeyFromFile $PublicKeyPath }

if (-not $keyLine) {
    if (-not $Controller) {
        $Controller = Read-Host 'Sysible Controller address (host or URL; blank = offline paste)'
    }
    $baseUrl = Resolve-ControllerUrl $Controller
    if ($baseUrl) {
        if (-not $PSBoundParameters.ContainsKey('Token') -and -not $Token) {
            $Token = Read-Host 'Relay enrollment token (from console: Host Enrollment -> Relay Enrollment -> Enroll a bastion; blank = offline)'
        }
        # If the operator pasted the relay PUBLIC key here instead of an enrollment
        # token (an easy mix-up), just use it as the offline key rather than sending
        # it to the Controller as a bogus token (which 403s).
        if ($Token -and $Token.Trim() -match '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-|sk-ssh-ed25519@|sk-ecdsa-)') {
            Write-Warn2 "That looks like a public key, not an enrollment token -- using it directly (offline)."
            $keyLine = Test-PublicKeyLine $Token
            $Token = ""
        }
        if ($Token) {
            try {
                $boot = Invoke-Bootstrap -baseUrl $baseUrl -token $Token -certPath $ControllerCert
                $keyLine = Test-PublicKeyLine $boot.relay_pubkey
                if ($boot.PSObject.Properties.Name -contains 'relay_user' -and $boot.relay_user) {
                    # Re-validate: this value came from the network, not parameter binding.
                    $User = Test-RelayUserName ([string]$boot.relay_user)
                }
                if ($boot.PSObject.Properties.Name -contains 'permitopen' -and $boot.permitopen) { $suggested = @($boot.permitopen) }
                if ($boot.PSObject.Properties.Name -contains 'bastion_source_ip' -and $boot.bastion_source_ip) {
                    $script:BootSourceIp = [string]$boot.bastion_source_ip
                }
                # Dedicated agent-tunnel key for hosts BEHIND this bastion (optional). Validated
                # like any untrusted controller value; a malformed one is dropped with a warning
                # so the relay setup still completes.
                if ($boot.PSObject.Properties.Name -contains 'agent_tunnel_pubkey' -and $boot.agent_tunnel_pubkey) {
                    try { $script:AgentTunnelKey = Test-PublicKeyLine ([string]$boot.agent_tunnel_pubkey) }
                    catch { Write-Warn2 "Ignoring malformed agent_tunnel_pubkey from controller: $($_.Exception.Message)" }
                }
                Write-Ok "Bootstrapped from Controller (relay key retrieved; bastion registered)"
            } catch {
                Write-Warn2 "Bootstrap failed: $($_.Exception.Message)"
                Write-Warn2 "Falling back to OFFLINE mode."
            }
        }
    }
}

if (-not $keyLine) {
    Write-Host ''
    Write-Host 'OFFLINE mode. On the Controller run:' -ForegroundColor White
    Write-Host '    sudo cat /opt/sysible/relay_keys/relay_ed25519.pub' -ForegroundColor Gray
    Write-Host 'and paste the single line below.' -ForegroundColor White
    $keyLine = Test-PublicKeyLine (Read-Host 'Relay PUBLIC key')
}

# PermitOpen = suggested-from-bootstrap + explicit, validated. Track whether an 'any'
# token originated from the bootstrap RESPONSE (untrusted network) vs an operator arg, so
# a MITM'd controller can't silently obtain an unrestricted pivot (confirmed below).
$permitOpenClean = @()
$anyFromBootstrap = $false
$mergedTargets = @()
foreach ($t in @($suggested))  { $v = ("$t").Trim(); if ($v) { $mergedTargets += $v; if ($v -eq 'any') { $anyFromBootstrap = $true } } }
foreach ($t in @($PermitOpen)) { $v = ("$t").Trim(); if ($v) { $mergedTargets += $v } }
if ($mergedTargets.Count -gt 0) {
    if (@($mergedTargets | Where-Object { $_ -eq 'any' }).Count -gt 0) {
        # Only honor 'any' when the WHOLE merged list is exactly 'any' (mirrors the Linux
        # validator). A lone 'any' mixed with real targets is a mistake, not a request for
        # an unrestricted pivot -- fail closed rather than let 'any' short-circuit and win.
        if ($mergedTargets.Count -ne 1) {
            throw "PermitOpen 'any' cannot be combined with specific host:port targets. Pass either 'any' alone (unrestricted pivot, not recommended) or an explicit host:port list."
        }
        $permitOpenClean = @('any')
    } else {
        foreach ($val in $mergedTargets) {
            if (-not (Test-PermitOpenTarget $val)) {
                throw "Invalid target '$val'. Use host:port (e.g. 192.168.1.50:22) or 'any'. OpenSSH PermitOpen is per host:port only -- no CIDR/subnet, single port 1-65535."
            }
            if ($permitOpenClean -notcontains $val) { $permitOpenClean += $val }
        }
    }
}
if (-not $permitOpenClean) {
    Write-Host ''
    Write-Host 'Which internal targets may this relay FORWARD to?' -ForegroundColor White
    Write-Host '  Enter one or more IP-or-hostname:port entries (NOT a URL), comma-separated.' -ForegroundColor Gray
    Write-Host '  This is the sshd PermitOpen allowlist -- the exact host:port pairs the' -ForegroundColor Gray
    Write-Host '  Controller may tunnel THROUGH this jump box to. Examples:' -ForegroundColor Gray
    Write-Host '      192.168.1.29:9000                 the Controller (agent egress back to it)' -ForegroundColor Gray
    Write-Host '      192.168.8.50:22, 192.168.8.51:22  specific hosts, SSH' -ForegroundColor Gray
    Write-Host '  Note: OpenSSH PermitOpen does NOT accept CIDR/subnets -- list each host:port.' -ForegroundColor Gray
    Write-Host '  For MANY subnets, list a target per host, or type "any" (allows ALL host:port' -ForegroundColor Gray
    Write-Host '  -- an unrestricted pivot, not recommended).' -ForegroundColor Gray
    $ans = Read-Host 'Targets (host:port[,host:port...] or "any")'
    foreach ($t in $ans.Split(',')) {
        $val = $t.Trim(); if (-not $val) { continue }
        if ($val -eq 'any') { $permitOpenClean = @('any'); break }
        if (-not (Test-PermitOpenTarget $val)) { throw "Invalid target '$val' (want host:port, single port 1-65535; no CIDR/subnet)." }
        if ($permitOpenClean -notcontains $val) { $permitOpenClean += $val }
    }
}
if (-not $permitOpenClean) {
    # Fail closed: a bastion that forwards to ANYTHING is an unrestricted pivot.
    throw "No forwarding targets given. Re-run with -PermitOpen host:port[,host:port...] so the relay account can only reach those internal targets. To deliberately allow all (not recommended), pass -PermitOpen any."
}
if ($permitOpenClean.Count -eq 1 -and $permitOpenClean[0] -eq 'any') {
    Write-Warn2 "PermitOpen=any: the relay account may forward to ANY reachable host:port -- an unrestricted pivot. Not recommended."
    if ($anyFromBootstrap) {
        # 'any' arrived in the bootstrap RESPONSE, not as an operator argument. A MITM'd or
        # compromised Controller must not silently obtain an unrestricted pivot -- require
        # an explicit, interactive confirmation before writing 'PermitOpen any'.
        Write-Warn2 "This unrestricted 'any' was SUGGESTED BY THE CONTROLLER during bootstrap, not typed by you."
        $ans = Read-Host "Allow the Controller to open an UNRESTRICTED pivot (PermitOpen any)? [y/N]"
        if ($ans -notmatch '^(y|yes)$') {
            throw "Refused controller-suggested 'PermitOpen any'. Re-run with -PermitOpen host:port[,host:port...] to restrict the relay account."
        }
    }
}

Write-Host ''
Write-Host ("  account   : {0} (non-admin, key-only, forwarding-only)" -f $User)
Write-Host ("  ssh port  : {0}" -f $Port)
Write-Host ("  permitopen: {0}" -f ($permitOpenClean -join ', '))
Write-Host ''

# ------------------------------------------------------- 1. OpenSSH Server ----
Write-Step 'Ensuring OpenSSH Server is installed and running'
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction SilentlyContinue
if ($cap -and $cap.State -ne 'Installed') {
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    Write-Ok "Installed $($cap.Name)"
} elseif ($cap) {
    Write-Ok 'OpenSSH Server already installed'
} else {
    Write-Warn2 'Could not query OpenSSH capability (older Windows?). Assuming sshd is present.'
}
Set-Service -Name sshd -StartupType Automatic
if ((Get-Service sshd).Status -ne 'Running') { Start-Service sshd }
Write-Ok 'sshd is running and set to start automatically'

$fwName = "OpenSSH-Server-In-TCP-$Port"
if (-not (Get-NetFirewallRule -Name $fwName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name $fwName -DisplayName "OpenSSH Server (sshd) TCP $Port" `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort $Port | Out-Null
    Write-Ok "Opened firewall for TCP $Port"
} else {
    Write-Ok "Firewall rule for TCP $Port already present"
}

# ------------------------------------------------------- 2. relay account -----
Write-Step "Ensuring least-privilege account '$User' exists"
$existing = Get-LocalUser -Name $User -ErrorAction SilentlyContinue
if (-not $existing) {
    $pw = ConvertTo-SecureString (New-StrongPassword) -AsPlainText -Force
    # NOTE: Windows New-LocalUser caps -Description at 48 characters.
    New-LocalUser -Name $User -Password $pw -FullName 'Sysible Relay (forwarding only)' `
        -Description 'Sysible Relay forwarding-only account' `
        -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
    Write-Ok "Created local user '$User'"
} else {
    Write-Ok "Local user '$User' already exists"
}
# Resolve the local Administrators group by its well-known SID (S-1-5-32-544).
# Get-LocalGroup -SID is unreliable on some builds (localized Windows, certain
# Server SKUs) and throws "Group S-1-5-32-544 was not found" even though the group
# exists -- a known LocalAccounts-module bug. Fall back through: SID enumeration,
# then a SID->NTAccount translation, so a lookup quirk never crashes the run.
function Get-AdminGroupName {
    $sid = 'S-1-5-32-544'
    try {
        $g = Get-LocalGroup -SID $sid -ErrorAction Stop
        if ($g -and $g.Name) { return $g.Name }
    } catch { }
    try {
        $g = Get-LocalGroup -ErrorAction Stop |
            Where-Object { $_.SID -and $_.SID.Value -eq $sid } | Select-Object -First 1
        if ($g -and $g.Name) { return $g.Name }
    } catch { }
    try {
        $obj = New-Object System.Security.Principal.SecurityIdentifier($sid)
        $nt  = $obj.Translate([System.Security.Principal.NTAccount]).Value
        # Strip the "BUILTIN\" (or localized) domain prefix -> bare group name.
        return ($nt -split '\\')[-1]
    } catch { }
    return $null
}
$adminGroup = Get-AdminGroupName
if (-not $adminGroup) {
    Write-Warn2 "Could not resolve the local Administrators group -- skipping the admin-membership check. '$User' was just created as a standard user, so this is only relevant if it pre-existed."
} else {
    $isAdmin = Get-LocalGroupMember -Group $adminGroup -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "*\$User" -or $_.Name -eq $User }
    if ($isAdmin) {
        Remove-LocalGroupMember -Group $adminGroup -Member $User -ErrorAction SilentlyContinue
        Write-Warn2 "Removed '$User' from $adminGroup -- the relay account must be non-admin."
    }
    Write-Ok "'$User' is a standard (non-administrative) account"
}

# Put the relay account in a DEDICATED group. Many hardened / AD-joined boxes gate SSH
# with a global `AllowGroups` (or `AllowUsers`) in sshd_config, evaluated BEFORE any Match
# block -- so without being in an allowed group, the relay account is rejected pre-auth
# ("not allowed because none of user's groups are listed in AllowGroups"). A dedicated
# group lets us allow EXACTLY this account (least privilege) if such a directive exists.
$RelayGroup = 'sysible-relay'
# Idempotent: DON'T trust Get-LocalGroup (the LocalAccounts module gives false negatives on
# some builds -- the same quirk that bit the SID lookup). Just try to create it and treat an
# "already exists" (NameInUse) as success, so a re-run never crashes here.
try {
    New-LocalGroup -Name $RelayGroup -Description 'Sysible Relay SSH access' -ErrorAction Stop | Out-Null
    Write-Ok "Created local group '$RelayGroup'"
} catch {
    Write-Ok "Local group '$RelayGroup' already exists"
}
# Likewise: adding an existing member throws -- swallow it. Membership is what matters.
try {
    Add-LocalGroupMember -Group $RelayGroup -Member $User -ErrorAction Stop
} catch { }
Write-Ok "'$User' is a member of '$RelayGroup'"

# ------------------------------------------------------- 3. authorized key ----
$sshData = Join-Path $env:ProgramData 'ssh'
$akFile  = Join-Path $sshData ("{0}_authorized_keys" -f $User)
Write-Step "Installing Controller public key -> $akFile"
if (-not (Test-Path -LiteralPath $sshData)) { New-Item -ItemType Directory -Path $sshData -Force | Out-Null }
# Authorize the relay key (controller-initiated ProxyJump) and, when the controller
# supplied it, the dedicated agent-tunnel key (agents BEHIND this bastion tunnel with
# THAT key, not the relay key). Overwrite -- never append -- so a re-run replaces both
# cleanly with no duplicates and a rotated key takes effect.
$akLines = @($keyLine)
if ($script:AgentTunnelKey) { $akLines += $script:AgentTunnelKey }
Set-Content -LiteralPath $akFile -Value ($akLines -join "`n") -Encoding ascii -NoNewline
Add-Content -LiteralPath $akFile -Value "`n" -Encoding ascii
if ($script:AgentTunnelKey) { Write-Ok "Authorized the relay key + the agent-tunnel key (for hosts behind this bastion)" }
# Make the DACL AUTHORITATIVE: reset any pre-existing (possibly attacker-planted)
# explicit ACEs, drop inheritance, set the owner to Administrators, and grant ONLY
# SYSTEM + Administrators (/grant:r replaces, not adds). icacls is a native exe, so
# check $LASTEXITCODE -- a failed lockdown must not pass silently.
function Invoke-Icacls { param([string[]]$IcaclsArgs)
    & icacls @IcaclsArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed ($LASTEXITCODE) on: icacls $($IcaclsArgs -join ' ')" }
}
Invoke-Icacls @($akFile, '/reset')
Invoke-Icacls @($akFile, '/setowner', 'Administrators')
Invoke-Icacls @($akFile, '/inheritance:r')
Invoke-Icacls @($akFile, '/grant:r', 'SYSTEM:F')
Invoke-Icacls @($akFile, '/grant:r', 'Administrators:F')
Write-Ok 'Installed key with an authoritative SYSTEM/Administrators-only ACL'

# ------------------------------------------------------- 4. sshd_config --------
$cfg = Join-Path $sshData 'sshd_config'
Write-Step "Hardening sshd_config for '$User' (forwarding-only)"
# Fixed, username-INDEPENDENT markers so a re-run always strips the prior managed
# block even if the relay user changed between runs (otherwise a stale Match block
# for an old username would be left behind).
$begin = "# >>> sysible-relay managed block -- do not edit by hand >>>"
$end   = "# <<< sysible-relay managed block <<<"
$permitOpenLine = 'PermitOpen ' + ($permitOpenClean -join ' ')
$forceMsg = "sysible-relay: this account is for Sysible Controller port-forwarding only. No shell."
$block = @"
$begin
Match User $User
    PasswordAuthentication no
    PubkeyAuthentication yes
    AuthorizedKeysFile "$akFile"
    AllowTcpForwarding yes
    $permitOpenLine
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    GatewayPorts no
    ForceCommand powershell.exe -NoProfile -NonInteractive -Command "Write-Host '$forceMsg'; exit 1"
$end
"@

$original = ''
if (Test-Path -LiteralPath $cfg) { $original = Get-Content -LiteralPath $cfg -Raw }
$escBegin = [Regex]::Escape($begin)
$escEnd   = [Regex]::Escape($end)
$stripped = [Regex]::Replace($original, "(?ms)^$escBegin.*?^$escEnd\s*", '')

# If the existing config gates SSH by a GLOBAL AllowGroups/AllowUsers (evaluated before any
# Match block), the relay account is rejected pre-auth unless it's permitted. Append our
# dedicated group (and the user) to whichever directive is present -- append-only, so we
# never remove existing allow entries, and the original is backed up below.
function Add-ToAllowList {
    param([string]$Text, [string]$Directive, [string]$Value)
    $rx = "(?im)^(\s*$Directive\s+)([^\r\n]*)"
    $m = [Regex]::Match($Text, $rx)
    if (-not $m.Success) { return $Text }                              # directive absent: nothing to do
    if (($m.Groups[2].Value -split '\s+') -contains $Value) { return $Text }   # already allowed
    Write-Ok "Adding '$Value' to the existing sshd '$Directive' allow-list"
    return [Regex]::Replace($Text, $rx, ('${1}${2} ' + $Value))
}
$stripped = Add-ToAllowList $stripped 'AllowGroups' $RelayGroup
$stripped = Add-ToAllowList $stripped 'AllowUsers'  $User

$stripped = $stripped.TrimEnd() + "`r`n`r`n"
$newCfg   = $stripped + ($block -replace "`n", "`r`n") + "`r`n"
$backup = "$cfg.sysible.bak"
if ($original) { Set-Content -LiteralPath $backup -Value $original -Encoding ascii }
Set-Content -LiteralPath $cfg -Value $newCfg -Encoding ascii
Write-Ok "Wrote managed Match block (backup: $backup)"

# --------------------------------------------------------- 5. restart + verify -
Write-Step 'Validating config and restarting sshd'
$sshd = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
if (Test-Path -LiteralPath $sshd) {
    $test = & $sshd -t 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 'sshd -t reported a problem; restoring prior config and aborting restart:'
        $test | ForEach-Object { Write-Host "        $_" }
        if ($original) {
            Set-Content -LiteralPath $cfg -Value $original -Encoding ascii   # restore exact prior contents
        } else {
            Remove-Item -LiteralPath $cfg -ErrorAction SilentlyContinue      # there was no config before; don't leave a broken one
        }
        throw 'sshd config validation failed. No changes to the running service were made.'
    }
}
Restart-Service sshd
Write-Ok 'sshd restarted with the new configuration'

# ---------------------------------------------------------------- summary ------
# Two DIFFERENT addresses on a dual-homed jump box:
#   * CONTROLLER-FACING (relay_host): the NIC on the Controller's network. The Controller
#     dials the jump box here. On bootstrap the Controller reports the exact address it saw
#     us connect from ($BootSourceIp) and auto-sets relay_host to it -- authoritative.
#   * PIVOT-FACING (agent --bastion): the NIC on an internal network. Agents on THAT network
#     reach the jump box here. This differs per pivot network the box fronts.
$allIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
    Select-Object -ExpandProperty IPAddress)
$ctrlFacing = if ($script:BootSourceIp) { $script:BootSourceIp } else { '<this box''s IP on the Controller''s network>' }
Write-Host ''
Write-Host 'Done. This bastion is ready.' -ForegroundColor White
Write-Host ''
if ($script:BootSourceIp) {
    Write-Host ("Controller connects to this jump box at {0} (its NIC on the Controller's" -f $ctrlFacing) -ForegroundColor Green
    Write-Host  "network). The Controller auto-set relay_host to this on bootstrap -- nothing to do." -ForegroundColor Green
} else {
    Write-Host 'If you did NOT bootstrap, set these in the console (Host Enrollment -> Relay Enrollment):' -ForegroundColor White
    Write-Host ("    Relay / bastion host : {0}" -f $ctrlFacing)
    Write-Host  '                           ^ this box''s IP ON THE CONTROLLER''S NETWORK -- NOT a pivot IP.'
    Write-Host ("    Relay SSH user       : {0}" -f $User)
    Write-Host ("    Relay SSH port       : {0}" -f $Port)
    Write-Host  '    Relay identity key   : (the PRIVATE key on the Controller, e.g. /opt/sysible/relay_keys/relay_ed25519)'
    Write-Host  '    Relay OS             : auto'
}
Write-Host ''
Write-Host 'This box''s IPv4 addresses (pick the PIVOT-network one for agent --bastion):' -ForegroundColor White
foreach ($ip in $allIps) {
    $tag = if ($ip -eq $script:BootSourceIp) { '  <- Controller-facing (relay_host)' } else { '' }
    Write-Host ("    {0}{1}" -f $ip, $tag) -ForegroundColor Gray
}
Write-Host ''
Write-Host 'Enroll agents on the hosts BEHIND this bastion, pointing them at the PIVOT NIC:' -ForegroundColor White
Write-Host  '    ...enroll... --bastion <this box''s IP on the AGENTS'' network>' -ForegroundColor Gray
Write-Host ''
Write-Host 'Verify the full forward path FROM THE CONTROLLER (use the controller-facing IP):' -ForegroundColor White
Write-Host ("    sudo ssh -i /opt/sysible/relay_keys/relay_ed25519 -o BatchMode=yes \") -ForegroundColor Gray
Write-Host ("      -J {0}@{1} <user>@<internal-host> whoami" -f $User, $ctrlFacing) -ForegroundColor Gray
Write-Host ''
Write-Host 'If that hangs/fails on "channel open", widen -PermitOpen for the internal target and re-run.' -ForegroundColor Yellow
