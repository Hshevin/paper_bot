import SwiftUI
import AppKit

@main struct PaperHarborApp: App {
    @StateObject private var model = AppModel()
    var body: some Scene {
        WindowGroup("论文港 · PaperHarbor") {
            ContentView().environmentObject(model)
                .frame(minWidth: 1000, minHeight: 680)
                .onAppear { NSApplication.shared.activate(ignoringOtherApps: true) }
        }
        .defaultSize(width: 1320, height: 840)
        .commands {
            CommandGroup(after: .newItem) {
                Button("更新所有订阅") { model.perform { try await $0.run(force: true) } }
                    .keyboardShortcut("r").disabled(model.busy)
            }
        }
        Settings { SettingsView().environmentObject(model) }
    }
}
