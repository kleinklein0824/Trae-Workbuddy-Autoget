' Trae & Buddy 签到助手 —— 无控制台窗口启动
' 双击本文件即可静默启动 GUI（等同参考项目里双击 exe 的体验）。
Option Explicit

Dim fso, shell, here, py, probe, i
Dim candidates

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here

' 依次探测可用的 Python 解释器：先查固定安装路径，再回退到 PATH
candidates = Array( _
    "C:\Program Files\Python313\pythonw.exe", _
    "C:\Program Files\Python312\pythonw.exe", _
    "C:\Program Files\Python311\pythonw.exe", _
    "C:\Program Files\Python310\pythonw.exe", _
    shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"), _
    shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"), _
    shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"), _
    "pythonw.exe", _
    "pyw.exe")

py = ""
For i = 0 To UBound(candidates)
    If Len(py) = 0 Then
        probe = candidates(i)
        If fso.FileExists(probe) Then
            py = probe
        ElseIf InStr(probe, "\") = 0 Then
            ' 无路径 → 交给 PATH 解析；先确认能解析到，否则 Run 会静默失败
            Err.Clear
            On Error Resume Next
            Dim rc
            rc = shell.Run("cmd /c where " & probe & " >nul 2>nul", 0, True)
            On Error GoTo 0
            If rc = 0 Then
                py = probe
            End If
        End If
    End If
Next

If Len(py) = 0 Then
    MsgBox "未检测到 Python。" & vbCrLf & vbCrLf & _
           "请先安装 Python 3.10 或更高版本，安装时勾选 ""Add Python to PATH""。" & vbCrLf & _
           "下载地址: https://www.python.org/downloads/", _
           vbCritical, "Trae & Buddy 签到助手"
    WScript.Quit 1
End If

' 0 = 隐藏窗口, False = 不等待返回
shell.Run """" & py & """ """ & here & "\main.py""", 0, False
