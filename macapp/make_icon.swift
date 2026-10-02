// 生成 1024px 图标：深炭圆角方块 + 陶土橙播放三角
import Cocoa
let size = 1024.0
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
let inset = 100.0
let rect = NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
NSColor(srgbRed: 0x2B/255, green: 0x2A/255, blue: 0x27/255, alpha: 1).setFill()
NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185).fill()
let tri = NSBezierPath()
tri.move(to: NSPoint(x: 420, y: 330)); tri.line(to: NSPoint(x: 420, y: 694)); tri.line(to: NSPoint(x: 712, y: 512)); tri.close()
tri.lineJoinStyle = .round; tri.lineWidth = 60
NSColor(srgbRed: 0xD9/255, green: 0x77/255, blue: 0x57/255, alpha: 1).set()
tri.fill(); tri.stroke()
img.unlockFocus()
let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
