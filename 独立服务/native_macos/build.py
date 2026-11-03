from pathlib import Path
import plistlib,subprocess
root=Path(__file__).resolve().parents[1]; app=root/'.private/macos/native/言小办系统事项.app';contents=app/'Contents';binary=contents/'MacOS/NativeSecretary'
binary.parent.mkdir(parents=True,exist_ok=True)
info={'CFBundleIdentifier':'org.yanxiaoban.preview.commercial.native','CFBundleName':'言小办系统事项','CFBundleDisplayName':'言小办系统事项','CFBundleExecutable':'NativeSecretary','CFBundleVersion':'1','CFBundlePackageType':'APPL','LSUIElement':True,'NSRemindersFullAccessUsageDescription':'将你交给言小办的待办保存到苹果提醒事项，设置系统到点通知；仅操作言小办登记的条目。','NSCalendarsFullAccessUsageDescription':'将你交给言小办的日程保存到苹果日历，并修改或取消言小办登记的日程。'}
(contents/'Info.plist').write_bytes(plistlib.dumps(info))
subprocess.run(['xcrun','swiftc','-swift-version','5','-module-cache-path',str(root/'.private/macos/swift-cache'),str(root/'native_macos/NativeSecretary.swift'),'-o',str(binary),'-framework','Cocoa','-framework','EventKit'],check=True)
subprocess.run(['/usr/bin/codesign','--force','--sign','-','--identifier',info['CFBundleIdentifier'],str(app)],check=True)
print('言小办系统事项组件已构建')
