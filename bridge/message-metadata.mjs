import { chmod, mkdir, stat } from 'node:fs/promises'

export function messageMetadata(message, jid, sourceKey) {
  const participant = sourceKey.participant || sourceKey.participantAlt || ''
  const fromMe = Boolean(sourceKey.fromMe)
  return {
    sourceId: `${sourceKey.id}:${fromMe ? 'out' : 'in'}:${participant}`,
    sender: fromMe ? 'You' : message.pushName || participant || jid,
  }
}

export async function secureDirectory(directory) {
  await mkdir(directory, { recursive: true, mode: 0o700 })
  await chmod(directory, 0o700)
  const details = await stat(directory)
  if ((details.mode & 0o777) !== 0o700) throw new Error('Could not secure the WhatsApp session directory')
}