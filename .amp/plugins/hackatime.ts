import type { PluginAPI } from '@ampcode/plugin'
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { resolve } from 'node:path'

export const description = 'Reports orb agent activity on lectern to Hackatime with wakatime-cli.'

// Installed by .agents/setup; ~/.wakatime.cfg is written by .agents/resume.
const wakatime = resolve(homedir(), '.local/bin/wakatime-cli')
const repoRoot = resolve(import.meta.dir, '../..')

export default function (amp: PluginAPI) {
	// Only in orbs, so a local Amp session never reports with someone's personal wakatime config.
	if (process.env.AMP_ORB !== '1') return

	amp.on('tool.result', (event) => {
		if (!existsSync(wakatime)) return
		const modified = amp.helpers.filesModifiedByToolCall(event)
		const input = event.input
		const entity =
			(modified?.length ? amp.helpers.filePathFromURI(modified[0]) : undefined) ??
			(typeof input.path === 'string' ? input.path : undefined) ??
			(typeof input.workdir === 'string' ? input.workdir : undefined) ??
			repoRoot
		const args = ['--entity', entity, '--project', 'lectern', '--plugin', 'amp lectern-hackatime/1']
		if (modified?.length) args.push('--write')
		spawn(wakatime, args, { stdio: 'ignore', detached: true }).unref()
	})
}
