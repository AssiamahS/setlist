// Renders the app icon (1024x1024 PNG, transparent corners) with AppKit.
// Usage: swift gen_icon.swift OUTPUT.png
import AppKit

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon1024.png"
let size = NSSize(width: 1024, height: 1024)
let img = NSImage(size: size)
img.lockFocus()

let bg = NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: 1024, height: 1024),
                      xRadius: 230, yRadius: 230)
NSColor(calibratedRed: 0.047, green: 0.055, blue: 0.071, alpha: 1).setFill()  // #0c0e12
bg.fill()

let green = NSColor(calibratedRed: 0.20, green: 0.82, blue: 0.478, alpha: 1)  // #33d17a

func draw(_ text: String, y: CGFloat, color: NSColor) {
    let attrs: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: 236, weight: .heavy),
        .foregroundColor: color,
        .kern: 14,
    ]
    let s = NSAttributedString(string: text, attributes: attrs)
    let w = s.size().width
    s.draw(at: NSPoint(x: (1024 - w) / 2, y: y))
}

draw("SET", y: 520, color: .white)
draw("LIST", y: 260, color: green)

// cue-timestamp tick marks along the bottom, like the Time column
green.withAlphaComponent(0.85).setFill()
for (i, h) in [58, 96, 74, 120, 66, 104, 82].enumerated() {
    NSBezierPath(roundedRect: NSRect(x: 262 + i * 76, y: 118, width: 34, height: h),
                 xRadius: 10, yRadius: 10).fill()
}

img.unlockFocus()

guard let tiff = img.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else {
    fatalError("could not render icon")
}
try! png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
