
/change cron setup
go to ~/workspace/dcf_model/com.yxue.dcf_model.signals.plist and change the schedule

/apply the change of cron setup to macbook
```
cp /Users/yxue/workspace/dcf_model/com.yxue.dcf_model.signals.plist \
  /Users/yxue/Library/LaunchAgents/com.yxue.dcf_model.signals.plist
launchctl unload /Users/yxue/Library/LaunchAgents/com.yxue.dcf_model.signals.plist
launchctl load /Users/yxue/Library/LaunchAgents/com.yxue.dcf_model.signals.plist
```

/wake up laptop at 12:55pm so the cron is able to run at 1:05pm
sudo pmset repeat wakeorpoweron MTWRFSU 12:55:00

/send imessage
osascript -e 'tell application "Messages" to send "VIX signal fired" to buddy "+1XXXXXXXXXX" of (service 1 whose service type is iMessage)'

/send file
osascript -e 'tell application "Messages" to send (POSIX file "/path/to/file.csv") to buddy "+1XXXXXXXXXX" of (service 1 whose service type is iMessage)'
ex: 
osascript -e 'tell application "Messages" to send (POSIX file "/Users/yxue/workspace/dcf_model/vix_sell_signal/2026-01-09_vx_eod_report.txt") to buddy "+14155187720" of (service 1 whose service type is iMessage)'

osascript -e 'tell application "Messages"
    set svc to 1st service whose service type is iMessage
    set b to buddy "+14155187720" of svc
    send (POSIX file "/Users/yxue/workspace/dcf_model/vix_sell_signal/2026-01-09_vx_eod_report.txt") to b
end tell' 2>&1

