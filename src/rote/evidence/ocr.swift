// Prints "path<TAB>recognized text" for every image path given on the command line, using macOS Vision.
// `rote demo` compiles and runs it on macOS to check that no screenshot shows a seeded member value.
import Foundation
import Vision

for path in CommandLine.arguments.dropFirst() {
    let url = URL(fileURLWithPath: path)
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(url: url, options: [:])
    do {
        try handler.perform([request])
        let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        print("\(path)\t\(lines.joined(separator: " | "))")
    } catch {
        print("\(path)\tERROR \(error)")
    }
}
