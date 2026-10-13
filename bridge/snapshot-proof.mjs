import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { readFile, realpath } from 'node:fs/promises'
import path from 'node:path'

export async function fileHash(file) {
  const digest = createHash('sha256')
  for await (const chunk of createReadStream(file)) digest.update(chunk)
  return digest.digest('hex')
}

async function containedFile(root, relative) {
  if (typeof relative !== 'string' || !relative || path.isAbsolute(relative)) {
    throw new Error('Invalid snapshot file path')
  }
  const base = await realpath(root)
  const file = await realpath(path.join(base, relative))
  if (!file.startsWith(base + path.sep)) throw new Error('Snapshot path escapes its directory')
  return file
}

export async function verifyPairingSnapshot(backupRoot, name, identityFile) {
  if (typeof name !== 'string' || !/^\d{8}T\d{6}-[a-f0-9]{32}$/.test(name)) {
    throw new Error('A verified archive snapshot is required before pairing reset')
  }
  const root = path.join(backupRoot, name)
  const manifest = JSON.parse(await readFile(await containedFile(root, 'manifest.json'), 'utf8'))
  const identity = (await readFile(identityFile, 'ascii')).trim()
  if (manifest.format !== 1 || manifest.inventory?.archive_id !== identity) {
    throw new Error('Snapshot belongs to a different archive')
  }
  if (await fileHash(await containedFile(root, 'archive.db')) !== manifest.database_sha256) {
    throw new Error('Snapshot database checksum mismatch')
  }
  const copiedIdentity = (await readFile(await containedFile(root, 'archive.db.identity'), 'ascii')).trim()
  if (copiedIdentity !== identity) throw new Error('Snapshot identity mismatch')
  for (const [relative, digest] of Object.entries(manifest.inventory.media)) {
    if (await fileHash(await containedFile(path.join(root, 'media'), relative)) !== digest) {
      throw new Error('Snapshot media checksum mismatch')
    }
  }
  for (const [relative, digest] of Object.entries(manifest.pending_events)) {
    if (path.basename(relative) !== relative || !relative.endsWith('.json')) {
      throw new Error('Invalid pending event filename')
    }
    if (await fileHash(await containedFile(path.join(root, 'whatsapp-events'), relative)) !== digest) {
      throw new Error('Snapshot pending event checksum mismatch')
    }
  }
}
