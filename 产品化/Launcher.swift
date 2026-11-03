import Cocoa

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let fm = FileManager.default
let resources = Bundle.main.resourceURL!
let payload = resources.appendingPathComponent("payload", isDirectory: true)
let home = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/言小办开源预览", isDirectory: true)
do {
    // Bootstrap logs live outside the install root: Maintenance must own its
    // first creation and identity. Never pre-populate an unrecognised root.
    let logFolder = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/言小办开源预览", isDirectory: true)
    try fm.createDirectory(at: logFolder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    let python = payload.appendingPathComponent("runtime/python/bin/python3")
    guard fm.isExecutableFile(atPath: python.path) else { throw NSError(domain: "言小办", code: 1) }
    let process = Process()
    process.executableURL = python
    process.arguments = ["-u", payload.appendingPathComponent("installer/installer_server.py").path,
                         "--home", home.path, "--bundle", payload.path]
    process.environment = ["HOME": fm.homeDirectoryForCurrentUser.path,
                           "PATH": payload.appendingPathComponent("runtime/node/bin").path + ":/usr/bin:/bin:/usr/sbin:/sbin",
                           "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "en_US.UTF-8"]
    let log = logFolder.appendingPathComponent("启动.log")
    if !fm.fileExists(atPath: log.path) { fm.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
    let output = try FileHandle(forWritingTo: log)
    output.seekToEndOfFile()
    process.standardOutput = output
    process.standardError = output
    try process.run()
    Thread.sleep(forTimeInterval: 1.0)
    if !process.isRunning && process.terminationStatus != 0 { throw NSError(domain: "言小办", code: 2) }
} catch {
    let alert = NSAlert()
    alert.messageText = "安装助手暂未启动"
    alert.informativeText = "请确认安装包完整，再重新打开。原有助手和数据不会因此被替换。启动记录保存在“资源库/Logs/言小办开源预览”。"
    alert.addButton(withTitle: "知道了")
    alert.runModal()
}
