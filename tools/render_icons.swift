// GPL-3.0-only. Project-authored geometric pi icon; no fonts or external art.
// Mirrors assets/yanxiaoban-icon.svg. Run from repository root: swift tools/render_icons.swift
import AppKit
import CoreGraphics

for (size, name) in [(180, "yanxiaoban-touch.png"), (32, "yanxiaoban-favicon.png")] {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    let ctx = NSGraphicsContext(bitmapImageRep: bitmap)!.cgContext
    let scale = CGFloat(size) / 180
    ctx.scaleBy(x: scale, y: scale)
    ctx.translateBy(x: 0, y: 180)
    ctx.scaleBy(x: 1, y: -1)
    ctx.setFillColor(CGColor(red: 21/255, green: 53/255, blue: 44/255, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: 180, height: 180))
    ctx.setStrokeColor(CGColor(red: 201/255, green: 245/255, blue: 216/255, alpha: 1))
    ctx.setLineWidth(12); ctx.setLineCap(.round); ctx.setLineJoin(.round)
    ctx.move(to: CGPoint(x: 43, y: 57)); ctx.addLine(to: CGPoint(x: 137, y: 57))
    ctx.move(to: CGPoint(x: 64, y: 57)); ctx.addLine(to: CGPoint(x: 64, y: 125))
    ctx.addQuadCurve(to: CGPoint(x: 52, y: 139), control: CGPoint(x: 64, y: 139))
    ctx.move(to: CGPoint(x: 116, y: 57)); ctx.addLine(to: CGPoint(x: 116, y: 125))
    ctx.addQuadCurve(to: CGPoint(x: 130, y: 139), control: CGPoint(x: 116, y: 139))
    ctx.strokePath()
    let url = URL(fileURLWithPath: "独立服务/web_shared/" + name)
    try bitmap.representation(using: .png, properties: [:])!.write(to: url)
    print("Generated \(size)x\(size) \(name)")
}
