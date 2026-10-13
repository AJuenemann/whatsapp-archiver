import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileHash, verifyPairingSnapshot } from './snapshot-proof.mjs'

test('pairing proof rejects absent, wrong-archive and corrupted snapshots', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'archive-proof-'))
  try {
    const name = '20260101T000000-' + 'a'.repeat(32)
    const snapshot = path.join(root, name)
    await mkdir(path.join(snapshot, 'media'), { recursive: true })
    await mkdir(path.join(snapshot, 'whatsapp-events'))
    const identity = path.join(root, 'archive.db.identity')
    await writeFile(identity, 'archive-id\n')
    await writeFile(path.join(snapshot, 'archive.db.identity'), 'archive-id\n')
    await writeFile(path.join(snapshot, 'archive.db'), 'Test archive database')
    await writeFile(path.join(snapshot, 'media', 'file.bin'), 'Saved media')
    await writeFile(path.join(snapshot, 'whatsapp-events', 'pending.json'), '{}')
    const manifest = {
      format: 1,
      database_sha256: await fileHash(path.join(snapshot, 'archive.db')),
      inventory: { archive_id: 'archive-id', media: { 'file.bin': await fileHash(path.join(snapshot, 'media', 'file.bin')) } },
      pending_events: { 'pending.json': await fileHash(path.join(snapshot, 'whatsapp-events', 'pending.json')) },
    }
    await writeFile(path.join(snapshot, 'manifest.json'), JSON.stringify(manifest))
    await assert.rejects(verifyPairingSnapshot(root, undefined, identity), /required/)
    await assert.rejects(verifyPairingSnapshot(root, '../escape', identity), /required/)
    await verifyPairingSnapshot(root, name, identity)
    await writeFile(identity, 'another-archive')
    await assert.rejects(verifyPairingSnapshot(root, name, identity), /different archive/)
    await writeFile(identity, 'archive-id\n')
    await writeFile(path.join(snapshot, 'media', 'file.bin'), 'Corrupted')
    await assert.rejects(verifyPairingSnapshot(root, name, identity), /media checksum/)
    await writeFile(path.join(snapshot, 'media', 'file.bin'), 'Saved media')
    await writeFile(path.join(snapshot, 'archive.db'), 'Corrupted')
    await assert.rejects(verifyPairingSnapshot(root, name, identity), /database checksum/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('bridge verifies proof before removing only the auth directory', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  const start = source.indexOf('async function resetLocalPairing')
  const check = source.indexOf('await verifyPairingSnapshot', start)
  const removal = source.indexOf('await rm(authRoot', start)
  assert.ok(start >= 0 && check > start && removal > check)
  assert.match(source, /resetLocalPairing\(request\.headers\['x-archive-snapshot'\]\)/)
  assert.doesNotMatch(source, /rm\((dataRoot|mediaRoot|eventRoot|backupRoot)/)
  assert.match(source, /await link\(temporary, destination\)/)
})
