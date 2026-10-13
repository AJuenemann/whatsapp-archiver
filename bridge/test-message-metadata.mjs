import assert from 'node:assert/strict'
import { chmod, mkdir, mkdtemp, rm, stat } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { messageMetadata, secureDirectory } from './message-metadata.mjs'

test('normal inbound message has a stable source id and sender', () => {
  assert.deepEqual(
    messageMetadata(
      { pushName: 'Ada' },
      'group@g.us',
      { id: 'message-1', fromMe: false, participant: 'ada@s.whatsapp.net' },
    ),
    { sourceId: 'message-1:in:ada@s.whatsapp.net', sender: 'Ada' },
  )
})

test('existing session directory permissions are tightened to owner-only', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'wa-session-test-'))
  const directory = path.join(root, 'auth')
  try {
    await mkdir(directory, { mode: 0o777 })
    await chmod(directory, 0o755)
    await secureDirectory(directory)
    assert.equal((await stat(directory)).mode & 0o777, 0o700)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('outbound message is attributed to the archive account', () => {
  assert.deepEqual(
    messageMetadata({}, 'group@g.us', { id: 'message-2', fromMe: true }),
    { sourceId: 'message-2:out:', sender: 'You' },
  )
})