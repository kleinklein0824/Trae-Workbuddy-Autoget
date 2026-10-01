param(
  [Parameter(Mandatory=$true)][string]$Title,
  [Parameter(Mandatory=$true)][string]$Body
)
$ErrorActionPreference = 'SilentlyContinue'
# Windows 10/11 native toast (bottom-right)
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
  $t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $x = $t.GetElementsByTagName('text')
  $x.Item(0).AppendChild($t.CreateTextNode($Title)) | Out-Null
  $x.Item(1).AppendChild($t.CreateTextNode($Body)) | Out-Null
  $toast = New-Object Windows.UI.Notifications.ToastNotification $t
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($toast)
  exit 0
} catch { }
# Fallback: tray balloon tip
Add-Type -AssemblyName System.Windows.Forms | Out-Null
Add-Type -AssemblyName System.Drawing | Out-Null
$ni = New-Object System.Windows.Forms.NotifyIcon
$ni.Icon = [System.Drawing.SystemIcons]::Information
$ni.Visible = $true
$ni.ShowBalloonTip(10000, $Title, $Body, [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 12
$ni.Dispose()
