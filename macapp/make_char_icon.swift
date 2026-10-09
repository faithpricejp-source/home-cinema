// 自建 App 统一图标：纸白圆角方块 + 黑色繁体宋体单字 + 下方一条彩色细线（每个 App 一种颜色）。
// 用法：swift make_char_icon.swift <字> <线色 RRGGBB> <输出 png>
import Cocoa

let args = CommandLine.arguments
guard args.count == 4, let rgb = UInt32(args[2], radix: 16) else {
    FileHandle.standardError.write("用法：swift make_char_icon.swift <字> <RRGGBB> <输出 png>\n".data(using: .utf8)!)
    exit(2)
}
let size = 1024.0
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()

let inset = 96.0
NSColor(srgbRed: 0xFA / 255.0, green: 0xF8 / 255.0, blue: 0xF2 / 255.0, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset),
             xRadius: 200, yRadius: 200).fill()

var font = NSFont.systemFont(ofSize: 440, weight: .bold)
for name in ["STSongti-TC-Bold", "Songti TC Bold", "Songti SC Bold", "Hiragino Mincho ProN"] {
    if let f = NSFont(name: name, size: 440) { font = f; break }
}
let text = args[1] as NSString
let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.black]
let ts = text.size(withAttributes: attrs)
text.draw(at: NSPoint(x: (size - ts.width) / 2, y: 320), withAttributes: attrs)

NSColor(srgbRed: Double((rgb >> 16) & 0xFF) / 255, green: Double((rgb >> 8) & 0xFF) / 255,
        blue: Double(rgb & 0xFF) / 255, alpha: 1).set()
let line = NSBezierPath()
line.lineWidth = 10
line.move(to: NSPoint(x: 330, y: 250)); line.line(to: NSPoint(x: 694, y: 250))
line.stroke()

img.unlockFocus()
let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: args[3]))
