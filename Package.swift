// swift-tools-version: 5.10
import PackageDescription
let package=Package(name:"AudioBridge",platforms:[.macOS(.v14)],products:[.executable(name:"audio-bridge",targets:["AudioBridge"])],targets:[.executableTarget(name:"AudioBridge")])
