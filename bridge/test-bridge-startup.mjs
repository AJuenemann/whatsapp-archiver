import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'

test('bridge boot starts the WhatsApp socket', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  assert.match(
    source,
    /server\.listen\([^)]*\)[\s\S]*if\s*\(\s*!testMode\s*&&\s*process\.env\.WHATSAPP_BRIDGE_TEST_MODE\s*!==\s*'1'\s*\)\s*\{[\s\S]*startWhatsApp\s*\(/,
  )
})

test('local pairing reset is POST-only and guarded by logged-out state', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  const logoutGuard = source.indexOf("if (status.state !== 'logged_out')")
  const authRemoval = source.indexOf('await rm(authRoot', logoutGuard)

  assert.notEqual(logoutGuard, -1)
  assert.ok(authRemoval > logoutGuard)
  assert.match(source, /request\.method === 'POST' && pathname === '\/pairing\/reset'/)
  assert.match(source, /await startWhatsApp\(\)[\s\S]*return status/)
})

test('bridge preserves plain conversation text and skips other protocol messages', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  assert.match(source, /if \(typeof body === 'string'\) return body/)
  assert.match(source, /if \(protocol && !isDelete && !\(isEdit && protocol\.editedMessage\)\) return/)
  assert.match(source, /WhatsApp-Systemereignis/)
})

test('contact updates accept Baileys display-name alternatives', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  assert.match(source, /contact\.name \|\| contact\.notify \|\| contact\.verifiedName \|\| contact\.pushName/)
  assert.match(source, /if \(!isRawJid\(title\)\) chatNames\.set\(chat\.id, title\)/)
  assert.match(source, /lidPnMappings = \[\]/)
  assert.match(source, /await saveLidMappings\(lidPnMappings\)/)
  assert.match(source, /socket\.ev\.on\('lid-mapping\.update'/)
  assert.match(source, /if \(knownChatIds\.has\(id\)\) updates\.add\(id\)/)
})

test('LID resolution reads only stored reverse mappings in bounded batches', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  assert.match(source, /authKeys\.get\('lid-mapping', reverseKeys\)/)
  assert.match(source, /pathname === '\/resolve-lids'/)
  assert.match(source, /body\.jids\.length > 200/)
  assert.match(source, /response\.writeHead\(413\)/)
})

test('manual refresh loads a regular_low archive snapshot and regular contact changes', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  assert.match(source, /const archive = typeof update\.archive === 'boolean' \? update\.archive : update\.archived/)
  assert.match(source, /authKeys\.set\(\{ 'app-state-sync-version': \{ regular_low: null \} \}\)/)
  assert.match(source, /activeSocket\.resyncAppState\(\['regular_low'\], true\)/)
  assert.match(source, /activeSocket\.resyncAppState\(\['regular'\], false\)/)
  assert.match(source, /!versions\.regular_low/)
  assert.match(source, /pathname === '\/contacts\/refresh'/)
  assert.match(source, /status\.state === 'syncing_history'/)
})
