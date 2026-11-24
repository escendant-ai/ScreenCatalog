#if DEBUG
import Foundation
#if canImport(UIKit)
import UIKit
#endif

/// `GET /screenshot`: the app's own windows as a PNG.
@MainActor
enum Screenshot {

    /// Every window of the foreground scene, drawn bottom-up by window level (ties keep the scene's
    /// order) at the screen's native scale, then flattened over opaque black into 8-bit sRGB.
    /// A simulator's own screenshot composites system overlays (the home indicator, the Dynamic
    /// Island) into some captures and not others; this never sees them, so two captures of the
    /// same screen are the same bytes.
    ///
    /// Two passes because `drawHierarchy` draws nothing (and still returns true) into the opaque,
    /// standard-range context the output needs; it draws into the screen's own (extended, 16-bit)
    /// format. The second pass converts that to what the recording tool decodes.
    static func response() -> Data {
        #if canImport(UIKit) && !os(watchOS)
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        guard let scene = scenes.first(where: { $0.activationState == .foregroundActive }) ?? scenes.first else {
            return HTTPResponse.json(503, ["error": "no connected window scene"])
        }

        let start = ContinuousClock.now
        let bounds = scene.screen.bounds
        let scale = scene.screen.scale
        let windows = scene.windows.enumerated()
            .filter { !$0.element.isHidden && $0.element.alpha > 0 }
            .sorted { ($0.element.windowLevel.rawValue, $0.offset) < ($1.element.windowLevel.rawValue, $1.offset) }
            .map(\.element)

        let screenFormat = UIGraphicsImageRendererFormat.preferred()
        screenFormat.scale = scale
        var undrawn: [String] = []
        let screen = UIGraphicsImageRenderer(bounds: bounds, format: screenFormat).image { _ in
            for window in windows where !window.drawHierarchy(in: window.frame, afterScreenUpdates: true) {
                undrawn.append("\(type(of: window))")
            }
        }

        let outputFormat = UIGraphicsImageRendererFormat()
        outputFormat.scale = scale
        outputFormat.opaque = true
        outputFormat.preferredRange = .standard
        let png = UIGraphicsImageRenderer(bounds: bounds, format: outputFormat).pngData { context in
            UIColor.black.setFill()
            context.fill(bounds)
            screen.draw(in: bounds)
        }

        let elapsed = (ContinuousClock.now - start).components
        let milliseconds = elapsed.seconds * 1000 + elapsed.attoseconds / 1_000_000_000_000_000
        var headers = [
            "X-ScreenCatalog-Scale": "\(scale)",
            "X-ScreenCatalog-Size": "\(Int(bounds.width * scale))x\(Int(bounds.height * scale))",
            "X-ScreenCatalog-Windows": windows.map { "\(type(of: $0))" }.joined(separator: ","),
            "X-ScreenCatalog-Render-Ms": "\(milliseconds)"
        ]
        if !undrawn.isEmpty {
            headers["X-ScreenCatalog-Undrawn"] = undrawn.joined(separator: ",")
        }

        return HTTPResponse.make(200, contentType: "image/png", body: png, headers: headers)
        #else
        return HTTPResponse.json(503, ["error": "screenshots need UIKit"])
        #endif
    }
}
#endif
