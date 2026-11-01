const fs = require('node:fs'), path = require('node:path'), cp = require('node:child_process');
const dir = __dirname, root = path.resolve(dir, '..');
const source = fs.readFileSync(path.join(root, 'ecompilot_project_roadmap.md'), 'utf8');
const items = JSON.parse(fs.readFileSync(path.join(dir, '图表清单.json'), 'utf8'));
const blocks = [...source.matchAll(/```mermaid\s*\n([\s\S]*?)```/g)];
if (blocks.length !== items.length) throw new Error('图表数量与清单不一致，请更新清单。');
for (let i = 0; i < items.length; i++) {
  if (path.basename(items[i].name) !== items[i].name) throw new Error('名称不能包含目录');
  fs.writeFileSync(path.join(dir, items[i].name + '.mmd'), blocks[i][1]);
}
let index = 0;
const doc = source.replace(/```mermaid\s*\n([\s\S]*?)```/g, () => {
  const { name } = items[index++];
  return `![${name}](${name}.png)\n\n[查看可缩放矢量图](${name}.svg)`;
});
fs.writeFileSync(path.join(dir, '项目架构文档（图片版）.md'), doc);
console.log(`已同步 ${items.length} 张图表。`);
if (process.argv.includes('--render')) {
  const entry = require.resolve('@mermaid-js/mermaid-cli', { paths: [root] });
  const packageFile = path.resolve(path.dirname(entry), '..', 'package.json');
  const pkg = JSON.parse(fs.readFileSync(packageFile, 'utf8'));
  const cli = path.resolve(path.dirname(packageFile), typeof pkg.bin === 'string' ? pkg.bin : pkg.bin.mmdc);
  const localConfig = path.join(dir, '浏览器配置.json');
  for (const { name } of items) for (const ext of ['svg', 'png']) {
    const args = [cli, '-i', path.join(dir, name + '.mmd'), '-o', path.join(dir, name + '.' + ext), '-c', path.join(dir, '渲染配置.json'), '--size', '3200', '-s', '2', '-b', 'white'];
    if (fs.existsSync(localConfig)) args.push('-p', localConfig);
    const result = cp.spawnSync(process.execPath, args, { stdio: 'inherit' });
    if (result.error) throw result.error;
    if (result.status !== 0) process.exit(result.status || 1);
  }
}
