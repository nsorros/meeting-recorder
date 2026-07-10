// A tiny macOS notifier: posts a native notification using the modern
// UserNotifications framework, so the notification carries THIS app bundle's
// icon on the left — the thing osascript and terminal-notifier cannot do on
// current macOS. Ad-hoc signing is enough; no Apple Developer account required.
//
// Usage:
//   notifier --title "Hello" --message "World" [--subtitle "..."]
//            [--content-image /path/to/image.png] [--sound]

import Cocoa
import UserNotifications

func flag(_ name: String) -> String? {
    let args = CommandLine.arguments
    if let i = args.firstIndex(of: name), i + 1 < args.count { return args[i + 1] }
    return nil
}

func hasFlag(_ name: String) -> Bool { CommandLine.arguments.contains(name) }

final class Delegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching(_ note: Notification) {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in
            let content = UNMutableNotificationContent()
            content.title = flag("--title") ?? "Notification"
            if let subtitle = flag("--subtitle") { content.subtitle = subtitle }
            content.body = flag("--message") ?? ""
            if hasFlag("--sound") { content.sound = .default }
            if let path = flag("--content-image"),
               let attachment = try? UNNotificationAttachment(
                   identifier: "image", url: URL(fileURLWithPath: path)) {
                content.attachments = [attachment]
            }
            let request = UNNotificationRequest(
                identifier: UUID().uuidString, content: content, trigger: nil)
            center.add(request) { _ in
                // Give the daemon a moment to deliver, then quit.
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { NSApp.terminate(nil) }
            }
        }
    }

    // Present even if this (accessory) app is frontmost.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler handler: @escaping (UNNotificationPresentationOptions) -> Void) {
        handler([.banner, .sound])
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)  // no Dock icon
app.run()
