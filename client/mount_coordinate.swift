import CryptoKit
import Darwin
import Foundation

let args = CommandLine.arguments
guard args.count >= 9, let parent = Int32(args[2]) else { exit(64) }
let target = URL(fileURLWithPath: args[1])
let name = args[3]
let baseline = args[4]
let state = args[5]
let lockPath = args[6]
let action = args[7]
var coordinationError: NSError?
var code: Int32 = 74

func verify(_ parent: Int32, _ name: String, _ baseline: String) throws -> mode_t? {
    if baseline == "missing" {
        var unused = stat()
        return fstatat(parent, name, &unused, AT_SYMLINK_NOFOLLOW) != 0 && errno == ENOENT ? 0 : nil
    }
    let checked = openat(parent, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    guard checked >= 0 else { return nil }
    let original = FileHandle(fileDescriptor: checked, closeOnDealloc: true)
    var info = stat()
    guard fstat(checked, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1,
          info.st_mode & 0o222 != 0 else { return nil }
    var hasher = SHA256()
    while let chunk = try original.read(upToCount: 65_536), !chunk.isEmpty {
        hasher.update(data: chunk)
    }
    let version = hasher.finalize().map { String(format: "%02x", $0) }.joined()
    return version == baseline ? info.st_mode & 0o777 : nil
}

func admitted(_ body: () throws -> Int32) {
    let lock = open(lockPath, O_CREAT | O_RDWR, 0o600)
    guard lock >= 0 else { return }
    defer { close(lock) }
    guard flock(lock, LOCK_EX) == 0 else { return }
    defer { flock(lock, LOCK_UN) }

    do {
        guard try String(contentsOfFile: state, encoding: .utf8) == "pending" else {
            code = 4
            return
        }
        code = try body()
    } catch {
        FileHandle.standardError.write(Data("\(error)\n".utf8))
    }
}

let coordinator = NSFileCoordinator(filePresenter: nil)
if action == "rename" {
    guard args.count == 12, let destination = Int32(args[9]) else { exit(64) }
    let other = URL(fileURLWithPath: args[8])
    let otherName = args[10]
    let otherBaseline = args[11]
    coordinator.coordinate(writingItemAt: target, options: .forMoving,
                           writingItemAt: other, options: .forReplacing,
                           error: &coordinationError) { _, _ in
        admitted {
            guard try verify(parent, name, baseline) != nil,
                  try verify(destination, otherName, otherBaseline) != nil else { return 3 }
            if otherBaseline == "missing" {
                if renameatx_np(parent, name, destination, otherName, UInt32(RENAME_EXCL)) == 0 { return 0 }
                return errno == EEXIST ? 3 : 74
            }
            return renameat(parent, name, destination, otherName) == 0 ? 0 : 74
        }
    }
} else {
    guard args.count == 9 else { exit(64) }
    let draft = args[8]
    coordinator.coordinate(writingItemAt: target, options: action == "delete" ? .forDeleting : .forReplacing,
                           error: &coordinationError) { _ in
        admitted {
            guard let mode = try verify(parent, name, baseline) else { return 3 }
            if action == "delete" {
                return unlinkat(parent, name, 0) == 0 ? 0 : 74
            }
            guard chmod(draft, mode) == 0 else { return 74 }
            return renameat(AT_FDCWD, draft, parent, name) == 0 ? 0 : 74
        }
    }
}
exit(coordinationError == nil ? code : 74)
