// Basic activation contract without launching a VS Code extension host.
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const commands = new Map();
const errors = [];
const vscode = {
    languages: { createDiagnosticCollection: () => ({ dispose() {} }) },
    commands: { registerCommand: (name, fn) => { commands.set(name, fn); return { dispose() {} }; } },
    window: { activeTextEditor: undefined, showErrorMessage: message => errors.push(message) },
};
const exportsObject = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../out/extension.js'), 'utf8'), {
    exports: exportsObject,
    require: name => name === 'vscode' ? vscode : require(name),
    Buffer, URL, TextEncoder,
});
exportsObject.activate({ subscriptions: [] });
const manifest = require('../package.json');
assert.deepStrictEqual([...commands.keys()].sort(), manifest.contributes.commands.map(c => c.command).sort());
(async () => {
    for (const command of commands.values()) await command();
    assert.strictEqual(errors.length, 4);
    assert(errors.every(message => message.includes('Python')));
    console.log('PASS: four commands register and handle missing editor');
})().catch(error => { console.error(error); process.exitCode = 1; });
