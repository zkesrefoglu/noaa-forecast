Attribute VB_Name = "VendorBackfill"
Option Explicit

' Vendor CSV backfill -- extracts the WGL Hourly_Temperatures_Report CSV
' attachments from Outlook and saves them to a local folder, named by the
' email's received date.
'
' Output naming:
'   <YYYY-MM-DD>.csv      morning report (received before noon, ~8:39 AM)
'   <YYYY-MM-DD>_pm.csv   afternoon report (received after noon, ~3:39 PM)
'
' Existing files in the target folder are overwritten -- safe to re-run.
'
' Run instructions:
'   1. Open Outlook desktop on the work laptop (corp network).
'   2. Alt+F11 to open the VBA editor.
'   3. Insert -> Module. Paste this entire file. F5 (or Run -> Run Sub).
'   4. Wait for the completion message box.

Public Sub BackfillVendorCSVs()
    On Error GoTo ErrorHandler

    Const TARGET_FOLDER  As String = "C:\Users\ke11982\OneDrive - Washington Gas Light Company\Desktop\\vendor_backfill\"
    Const SUBJECT_PREFIX As String = "Hourly_Temperatures_Report"   ' actual subject is e.g. "Hourly_Temperatures_Report (9672 rows found)"
    Const SENDER_HINT    As String = "auto_man"   ' fuzzy match -- SenderEmailAddress format varies in Exchange

    ' Ensure target folder exists
    If Dir(TARGET_FOLDER, vbDirectory) = "" Then
        MkDir TARGET_FOLDER
    End If

    Dim ns As Outlook.NameSpace
    Set ns = Application.GetNamespace("MAPI")

    Dim inbox As Outlook.Folder
    Set inbox = ns.GetDefaultFolder(olFolderInbox)

    ' Restrict to subjects starting with the prefix using an alphabetical
    ' range. e.g. for "Hourly_Temperatures_Report" the upper bound becomes
    ' "Hourly_Temperatures_Reporu" -- catches any subject in
    ' [prefix, prefix-with-last-char-incremented).
    Dim upperBound As String
    upperBound = Left(SUBJECT_PREFIX, Len(SUBJECT_PREFIX) - 1) & _
                 Chr(Asc(Right(SUBJECT_PREFIX, 1)) + 1)

    Dim filtered As Outlook.Items
    Set filtered = inbox.Items.Restrict( _
        "[Subject] >= '" & SUBJECT_PREFIX & "' AND " & _
        "[Subject] < '" & upperBound & "'")

    Dim totalFound As Long, savedAM As Long, savedPM As Long
    Dim noAttach As Long, senderMiss As Long, nonMail As Long
    Dim i As Long
    Dim msg As Object
    Dim atch As Outlook.Attachment
    Dim recvTime As Date
    Dim dateStr As String, suffix As String, fpath As String
    Dim foundCsv As Boolean

    totalFound = filtered.Count
    Debug.Print "Subject-filtered email count: " & totalFound

    For i = 1 To totalFound
        Set msg = filtered.Item(i)

        If TypeName(msg) <> "MailItem" Then
            nonMail = nonMail + 1
            GoTo NextItem
        End If

        ' Fuzzy sender check (handles SMTP and Exchange LegacyExchangeDN)
        If InStr(1, LCase(msg.SenderEmailAddress), SENDER_HINT, vbTextCompare) = 0 Then
            senderMiss = senderMiss + 1
            GoTo NextItem
        End If

        recvTime = msg.ReceivedTime
        dateStr = Format(recvTime, "yyyy-mm-dd")
        If Hour(recvTime) < 12 Then
            suffix = ""
        Else
            suffix = "_pm"
        End If

        foundCsv = False
        For Each atch In msg.Attachments
            If LCase(Right(atch.FileName, 4)) = ".csv" Then
                fpath = TARGET_FOLDER & dateStr & suffix & ".csv"
                atch.SaveAsFile fpath
                foundCsv = True
                If suffix = "" Then
                    savedAM = savedAM + 1
                Else
                    savedPM = savedPM + 1
                End If
                Exit For
            End If
        Next atch

        If Not foundCsv Then noAttach = noAttach + 1

NextItem:
    Next i

    MsgBox "Vendor CSV backfill complete." & vbCrLf & vbCrLf & _
           "Emails matched (subject): " & totalFound & vbCrLf & _
           "Morning CSVs saved:       " & savedAM & vbCrLf & _
           "Afternoon CSVs saved:     " & savedPM & vbCrLf & _
           "No CSV attachment:        " & noAttach & vbCrLf & _
           "Sender mismatch:          " & senderMiss & vbCrLf & _
           "Non-mail items skipped:   " & nonMail & vbCrLf & vbCrLf & _
           "Saved to: " & TARGET_FOLDER, _
           vbInformation, "Vendor CSV Backfill"
    Exit Sub

ErrorHandler:
    MsgBox "Error " & Err.Number & ": " & Err.Description & vbCrLf & _
           "Last item index: " & i & " of " & totalFound, _
           vbCritical, "Backfill failed"
End Sub
