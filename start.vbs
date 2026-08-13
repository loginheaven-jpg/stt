' Batasseugi launcher. Runs the app with no console window.
' ASCII only, no Korean, no hard-coded paths. See CLAUDE.md for why.
Option Explicit
Dim sh, fso, here
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
On Error Resume Next
sh.Run "pythonw.exe """ & here & "\app.py""", 0, False
If Err.Number <> 0 Then
  MsgBox "Python was not found. Install Python 3.10 or newer and be sure to check Add Python to PATH.", 48, "Batasseugi"
End If
