// Copyright 2026 Mrunal Mohadikar
// SPDX-License-Identifier: Apache-2.0
import Foundation
import ScreenCaptureKit
import CoreMedia
import AudioToolbox
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import Vision
import PDFKit

final class AudioWriter: NSObject, SCStreamOutput, SCStreamDelegate {
    private let base = URL(fileURLWithPath: "/tmp/thinkingpartner", isDirectory: true)
    private let lock = NSLock()
    private var printedSystem = false
    private var printedMic = false
    private var printedScreen = false
    private let ciContext = CIContext(options: [.useSoftwareRenderer: false])

    override init() {
        super.init()
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        for name in ["system.pcm", "mic.pcm"] {
            FileManager.default.createFile(
                atPath: base.appendingPathComponent(name).path,
                contents: Data()
            )
        }
        try? FileManager.default.removeItem(at: base.appendingPathComponent("screenshot.request"))
    }

    private func append(_ samples: [Int16], to filename: String) {
        guard !samples.isEmpty else { return }
        let url = base.appendingPathComponent(filename)
        let data = samples.withUnsafeBytes { Data($0) }
        lock.lock()
        defer { lock.unlock() }
        do {
            let h = try FileHandle(forWritingTo: url)
            _ = try h.seekToEnd()
            try h.write(contentsOf: data)
            try h.close()
        } catch {
            print("WRITE ERROR \(filename): \(error)")
            fflush(stdout)
        }
    }

    private func withAudioBuffers<T>(
        _ sb: CMSampleBuffer,
        _ body: (UnsafeMutableAudioBufferListPointer) -> T?
    ) -> T? {
        guard let fd = CMSampleBufferGetFormatDescription(sb),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fd)?.pointee else {
            return nil
        }
        let channels = max(1, Int(asbd.mChannelsPerFrame))
        let size = MemoryLayout<AudioBufferList>.size +
            max(0, channels - 1) * MemoryLayout<AudioBuffer>.size
        let raw = UnsafeMutableRawPointer.allocate(
            byteCount: size,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { raw.deallocate() }
        let abl = raw.bindMemory(to: AudioBufferList.self, capacity: 1)
        var retainedBlock: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb,
            bufferListSizeNeededOut: nil,
            bufferListOut: abl,
            bufferListSize: size,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment),
            blockBufferOut: &retainedBlock
        )
        guard status == noErr else {
            print("AUDIO BUFFER ERROR: \(status)")
            fflush(stdout)
            return nil
        }
        return body(UnsafeMutableAudioBufferListPointer(abl))
    }

    private func handleSystem(_ sb: CMSampleBuffer) {
        _ = withAudioBuffers(sb) { bufs -> Bool? in
            guard bufs.count >= 1, let leftData = bufs[0].mData else { return nil }
            let frames = Int(bufs[0].mDataByteSize) / MemoryLayout<Float>.size
            guard frames > 0 else { return nil }
            let left = leftData.bindMemory(to: Float.self, capacity: frames)
            let right: UnsafeMutablePointer<Float>? =
                bufs.count > 1 && bufs[1].mData != nil
                ? bufs[1].mData!.bindMemory(to: Float.self, capacity: frames) : nil

            var out = [Int16]()
            out.reserveCapacity(frames / 3 + 1)
            var i = 0
            while i + 2 < frames {
                var sum: Float = 0
                for k in 0..<3 {
                    let l = left[i+k]
                    sum += right != nil ? (l + right![i+k]) * 0.5 : l
                }
                let v = max(-1.0, min(1.0, sum / 3.0))
                out.append(Int16(v * 32767.0))
                i += 3
            }
            append(out, to: "system.pcm")
            return true
        }
    }

    private func handleMic(_ sb: CMSampleBuffer) {
        _ = withAudioBuffers(sb) { bufs -> Bool? in
            guard let b = bufs.first, let data = b.mData else { return nil }
            let count = Int(b.mDataByteSize) / MemoryLayout<Int16>.size
            guard count > 0 else { return nil }
            let ptr = data.bindMemory(to: Int16.self, capacity: count)
            append(Array(UnsafeBufferPointer(start: ptr, count: count)), to: "mic.pcm")
            return true
        }
    }

    private func handleScreen(_ sb: CMSampleBuffer) {
        let request = base.appendingPathComponent("screenshot.request")
        guard FileManager.default.fileExists(atPath: request.path) else { return }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sb) else { return }

        let image = CIImage(cvPixelBuffer: pixelBuffer)
        guard let cgImage = ciContext.createCGImage(image, from: image.extent) else { return }

        let finalURL = base.appendingPathComponent("screenshot.jpg")
        let tempURL = base.appendingPathComponent("screenshot.tmp.jpg")
        guard let destination = CGImageDestinationCreateWithURL(
            tempURL as CFURL,
            UTType.jpeg.identifier as CFString,
            1,
            nil
        ) else { return }

        CGImageDestinationAddImage(destination, cgImage, [
            kCGImageDestinationLossyCompressionQuality: 0.82
        ] as CFDictionary)

        guard CGImageDestinationFinalize(destination) else { return }

        lock.lock()
        defer { lock.unlock() }
        do {
            if FileManager.default.fileExists(atPath: finalURL.path) {
                try FileManager.default.removeItem(at: finalURL)
            }
            try FileManager.default.moveItem(at: tempURL, to: finalURL)
            try? FileManager.default.removeItem(at: request)
            print("Screenshot captured ✓")
            fflush(stdout)
        } catch {
            print("SCREENSHOT WRITE ERROR: \(error)")
            fflush(stdout)
        }
    }

    func stream(_ stream: SCStream,
                didOutputSampleBuffer sb: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard sb.isValid else { return }
        if type == .audio {
            if !printedSystem {
                printedSystem = true
                print("Receiving system audio buffers ✓")
                fflush(stdout)
            }
            handleSystem(sb)
        } else if #available(macOS 15.0, *), type == .microphone {
            if !printedMic {
                printedMic = true
                print("Receiving microphone buffers ✓")
                fflush(stdout)
            }
            handleMic(sb)
        } else if type == .screen {
            if !printedScreen {
                printedScreen = true
                print("Receiving screen buffers ✓")
                fflush(stdout)
            }
            handleScreen(sb)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        print("Capture stopped: \(error)")
        fflush(stdout)
        exit(2)
    }
}

/// `audio-bridge --qr <text> <out.png>` writes a QR code (used for the iPad pairing link).
func writeQRCode(_ text: String, to path: String) -> Bool {
    guard let filter = CIFilter(name: "CIQRCodeGenerator") else { return false }
    filter.setValue(Data(text.utf8), forKey: "inputMessage")
    filter.setValue("M", forKey: "inputCorrectionLevel")
    guard let output = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 12, y: 12)),
          let cg = CIContext().createCGImage(output, from: output.extent),
          let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL,
                                                     UTType.png.identifier as CFString, 1, nil)
    else { return false }
    CGImageDestinationAddImage(dest, cg, nil)
    return CGImageDestinationFinalize(dest)
}

/// `audio-bridge --ocr <file>` prints the text found in an image or a scanned PDF (on-device Vision OCR).
func recognizeText(in image: CGImage) -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    try? VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
}

func ocrFile(_ path: String) -> Bool {
    let url = URL(fileURLWithPath: path)
    var pages: [String] = []
    if url.pathExtension.lowercased() == "pdf" {
        guard let doc = PDFDocument(url: url) else { return false }
        for i in 0..<min(doc.pageCount, 50) {
            guard let page = doc.page(at: i) else { continue }
            let box = page.bounds(for: .mediaBox)
            let scale = 2.0
            let size = CGSize(width: box.width * scale, height: box.height * scale)
            guard let ctx = CGContext(data: nil, width: Int(size.width), height: Int(size.height),
                                      bitsPerComponent: 8, bytesPerRow: 0,
                                      space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { continue }
            ctx.setFillColor(CGColor(gray: 1, alpha: 1))
            ctx.fill(CGRect(origin: .zero, size: size))
            ctx.scaleBy(x: scale, y: scale)
            page.draw(with: .mediaBox, to: ctx)
            if let img = ctx.makeImage() { pages.append(recognizeText(in: img)) }
        }
    } else {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return false }
        pages.append(recognizeText(in: img))
    }
    print(pages.joined(separator: "\n\n"))
    return true
}

@main
struct Main {
    static func main() async {
        let args = CommandLine.arguments
        if args.count == 4 && args[1] == "--qr" {
            exit(writeQRCode(args[2], to: args[3]) ? 0 : 1)
        }
        if args.count == 3 && args[1] == "--ocr" {
            exit(ocrFile(args[2]) ? 0 : 1)
        }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let display = content.displays.first else {
                print("ERROR: No display found")
                return
            }

            let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
            let cfg = SCStreamConfiguration()

            // Capture enough pixels for readable case exhibits while keeping API images manageable.
            let maxWidth = 1920
            let scale = min(1.0, Double(maxWidth) / Double(display.width))
            cfg.width = max(2, Int(Double(display.width) * scale))
            cfg.height = max(2, Int(Double(display.height) * scale))
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: 4) // 4 fps locally; only requested frame is written
            cfg.queueDepth = 3

            cfg.capturesAudio = true
            cfg.excludesCurrentProcessAudio = true
            cfg.sampleRate = 48000
            cfg.channelCount = 2
            if #available(macOS 15.0, *) { cfg.captureMicrophone = true }

            let writer = AudioWriter()
            let stream = SCStream(filter: filter, configuration: cfg, delegate: writer)
            try stream.addStreamOutput(writer, type: .audio,
                sampleHandlerQueue: DispatchQueue(label: "system.audio"))
            if #available(macOS 15.0, *) {
                try stream.addStreamOutput(writer, type: .microphone,
                    sampleHandlerQueue: DispatchQueue(label: "microphone.audio"))
            }
            try stream.addStreamOutput(writer, type: .screen,
                sampleHandlerQueue: DispatchQueue(label: "screen.frames"))

            try await stream.startCapture()
            print("Audio + on-demand screenshot capture running. Path: /tmp/thinkingpartner")
            fflush(stdout)
            while true { try await Task.sleep(nanoseconds: 1_000_000_000) }
        } catch {
            print("ERROR: \(error)")
            fflush(stdout)
            exit(1)
        }
    }
}
