-- Meeting Recorder notifier applet.
--
-- Notifications posted via `osascript` always show the generic Script Editor
-- icon. To show the Meeting Recorder logo we post them from this tiny app
-- bundle instead: meeting_recorder.py drops "<title>\n<body>" files into the
-- notify-queue directory and opens this app, which drains the queue and posts
-- each notification under its own (custom) icon.

on processQueue()
    set homePath to POSIX path of (path to home folder)
    set qdir to homePath & ".local/state/meeting-recorder/notify-queue/"
    try
        do shell script "mkdir -p " & quoted form of qdir
        set listing to do shell script "ls -1 " & quoted form of qdir & " 2>/dev/null || true"
    on error
        return
    end try
    if listing is "" then return
    set oldTID to AppleScript's text item delimiters
    repeat with fn in paragraphs of listing
        set fname to fn as text
        if fname is not "" then
            set fpath to qdir & fname
            try
                set theData to do shell script "cat " & quoted form of fpath
                set AppleScript's text item delimiters to (ASCII character 10)
                set parts to text items of theData
                set theTitle to item 1 of parts
                if (count of parts) > 1 then
                    set theBody to (items 2 thru -1 of parts) as text
                else
                    set theBody to ""
                end if
                set AppleScript's text item delimiters to oldTID
                display notification theBody with title theTitle
                do shell script "rm -f " & quoted form of fpath
            end try
        end if
    end repeat
    set AppleScript's text item delimiters to oldTID
end processQueue

on run
    processQueue()
end run

on reopen
    processQueue()
end run
