"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || function (mod) {
    if (mod && mod.__esModule) return mod;
    var result = {};
    if (mod != null) for (var k in mod) if (k !== "default" && Object.prototype.hasOwnProperty.call(mod, k)) __createBinding(result, mod, k);
    __setModuleDefault(result, mod);
    return result;
};
var __awaiter = (this && this.__awaiter) || function (thisArg, _arguments, P, generator) {
    function adopt(value) { return value instanceof P ? value : new P(function (resolve) { resolve(value); }); }
    return new (P || (P = Promise))(function (resolve, reject) {
        function fulfilled(value) { try { step(generator.next(value)); } catch (e) { reject(e); } }
        function rejected(value) { try { step(generator["throw"](value)); } catch (e) { reject(e); } }
        function step(result) { result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected); }
        step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = void 0;
const vscode = __importStar(require("vscode"));
const http = __importStar(require("http"));
const https = __importStar(require("https"));
function postJson(urlString, body) {
    const url = new URL(urlString);
    const data = JSON.stringify(body !== null && body !== void 0 ? body : {});
    const isHttps = url.protocol === 'https:';
    const requestImpl = isHttps ? https.request : http.request;
    return new Promise((resolve, reject) => {
        const req = requestImpl({
            protocol: url.protocol,
            hostname: url.hostname,
            port: url.port ? Number(url.port) : (isHttps ? 443 : 80),
            path: `${url.pathname}${url.search}`,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json; charset=utf-8',
                'Content-Length': Buffer.byteLength(data),
            },
        }, (res) => {
            var _a;
            const statusCode = (_a = res.statusCode) !== null && _a !== void 0 ? _a : 0;
            const chunks = [];
            res.on('data', (chunk) => chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
            res.on('end', () => {
                const raw = Buffer.concat(chunks).toString('utf-8');
                if (statusCode < 200 || statusCode >= 300) {
                    reject(new Error(`HTTP ${statusCode}: ${raw || res.statusMessage || 'Request failed'}`));
                    return;
                }
                try {
                    resolve(JSON.parse(raw));
                }
                catch (e) {
                    reject(new Error(`响应不是合法 JSON：${raw.slice(0, 300)}`));
                }
            });
        });
        req.on('error', (err) => reject(err));
        req.setTimeout(360000, () => req.destroy(new Error('请求超时，请检查后台服务')));
        req.write(data);
        req.end();
    });
}
function parseFirstLineNumber(value) {
    if (!value)
        return undefined;
    const match = value.match(/\d+/);
    if (!match)
        return undefined;
    const n = Number(match[0]);
    if (!Number.isFinite(n))
        return undefined;
    return n;
}
function mapRiskToSeverity(level) {
    if (level === '高')
        return vscode.DiagnosticSeverity.Error;
    if (level === '中')
        return vscode.DiagnosticSeverity.Warning;
    return vscode.DiagnosticSeverity.Information;
}
function safeStringify(value) {
    const seen = new WeakSet();
    try {
        return JSON.stringify(value, (_key, v) => {
            if (v && typeof v === 'object') {
                if (seen.has(v))
                    return '[Circular]';
                seen.add(v);
            }
            return v;
        }, 2);
    }
    catch (_a) {
        return undefined;
    }
}
function toText(value) {
    if (typeof value === 'string')
        return value;
    if (value === null || value === undefined)
        return '';
    const json = safeStringify(value);
    if (typeof json === 'string')
        return json;
    try {
        return String(value);
    }
    catch (_a) {
        return '';
    }
}
function escapeHtml(value) {
    const unsafe = toText(value);
    return unsafe
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
function toJsonPretty(value) {
    const json = safeStringify(value);
    return json !== null && json !== void 0 ? json : toText(value);
}
function toJsonReport(value) {
    const json = safeStringify(value);
    if (typeof json === 'string')
        return json;
    return JSON.stringify({ text: toText(value) }, null, 2);
}
function normalizeReviewResult(input) {
    let data = input;
    if (typeof data === 'string') {
        try {
            data = JSON.parse(data);
        }
        catch (_a) {
            return { review_summary: data, issues: [] };
        }
    }
    if (data && typeof data === 'object' && 'result' in data) {
        data = data.result;
    }
    const issues = Array.isArray(data === null || data === void 0 ? void 0 : data.issues) ? data.issues : [];
    return {
        review_summary: typeof (data === null || data === void 0 ? void 0 : data.review_summary) === 'string' ? data.review_summary : toText(data === null || data === void 0 ? void 0 : data.review_summary),
        issues: issues.map((i) => ({
            dimension: typeof (i === null || i === void 0 ? void 0 : i.dimension) === 'string' ? i.dimension : toText(i === null || i === void 0 ? void 0 : i.dimension),
            line_number: typeof (i === null || i === void 0 ? void 0 : i.line_number) === 'string' ? i.line_number : toText(i === null || i === void 0 ? void 0 : i.line_number),
            violated_rule: typeof (i === null || i === void 0 ? void 0 : i.violated_rule) === 'string' ? i.violated_rule : toText(i === null || i === void 0 ? void 0 : i.violated_rule),
            risk_level: typeof (i === null || i === void 0 ? void 0 : i.risk_level) === 'string' ? i.risk_level : toText(i === null || i === void 0 ? void 0 : i.risk_level),
            risk_description: typeof (i === null || i === void 0 ? void 0 : i.risk_description) === 'string' ? i.risk_description : toText(i === null || i === void 0 ? void 0 : i.risk_description),
            fix_suggestion: typeof (i === null || i === void 0 ? void 0 : i.fix_suggestion) === 'string' ? i.fix_suggestion : toText(i === null || i === void 0 ? void 0 : i.fix_suggestion),
        })),
    };
}
function activate(context) {
    const diagnosticCollection = vscode.languages.createDiagnosticCollection('ai-reviewer');
    context.subscriptions.push(diagnosticCollection);
    const apiHost = () => (vscode.workspace.getConfiguration('aiReviewer').get('apiHost') || 'http://127.0.0.1:8000').replace(/\/+$/, '');
    // 编辑器场景默认 auto：既不是单 Agent，也不总是跑完整四领域。
    const strategy = () => {
        const value = (vscode.workspace.getConfiguration('aiReviewer').get('strategy') || 'auto').trim().toLowerCase();
        return value === 'fast' || value === 'full' ? value : 'auto';
    };
    const runReview = (editor, target) => __awaiter(this, void 0, void 0, function* () {
        yield vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: 'AI 正在多 Agent 审查代码...',
            cancellable: false,
        }, () => __awaiter(this, void 0, void 0, function* () {
            try {
                const data = yield postJson(`${apiHost()}/review?strategy=${encodeURIComponent(strategy())}`, { code: target.code });
                const result = normalizeReviewResult(data.result);
                applyDiagnostics(editor.document, result, diagnosticCollection, target.lineOffset, target.allowedStartLine, target.allowedEndLine, target.selectionRange);
                showReviewResult(result, () => toJsonReport(result), (line) => revealLine(editor, line), (content) => __awaiter(this, void 0, void 0, function* () { return saveReportToWorkspace(content); }), target.lineOffset);
            }
            catch (error) {
                vscode.window.showErrorMessage(`审查失败: ${error.message}`);
            }
        }));
    });
    const runOptimize = (editor, target) => __awaiter(this, void 0, void 0, function* () {
        const sourceVersion = editor.document.version;
        yield vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: 'AI 正在尝试优化代码...',
            cancellable: false,
        }, () => __awaiter(this, void 0, void 0, function* () {
            var _a;
            try {
                const data = yield postJson(`${apiHost()}/optimize?strategy=${encodeURIComponent(strategy())}`, { code: target.code });
                showOptimizeResult(target.code, (_a = data.result) !== null && _a !== void 0 ? _a : '', (optimized) => __awaiter(this, void 0, void 0, function* () {
                    if (editor.document.isClosed || editor.document.version !== sourceVersion) {
                        throw new Error('文件已修改或关闭，请重新生成优化方案');
                    }
                    if (target.selectionRange) {
                        yield replaceRange(editor, target.selectionRange, optimized);
                    }
                    else {
                        yield replaceWholeDocument(editor, optimized);
                    }
                }));
            }
            catch (error) {
                vscode.window.showErrorMessage(`优化失败: ${error.message}`);
            }
        }));
    });
    const reviewCommand = vscode.commands.registerCommand('ai-reviewer.review', () => __awaiter(this, void 0, void 0, function* () {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage('请先打开一个 Python 文件');
            return;
        }
        const target = getWholeDocumentTarget(editor);
        yield runReview(editor, target);
    }));
    const optimizeCommand = vscode.commands.registerCommand('ai-reviewer.optimize', () => __awaiter(this, void 0, void 0, function* () {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage('请先打开一个 Python 文件');
            return;
        }
        const target = getWholeDocumentTarget(editor);
        yield runOptimize(editor, target);
    }));
    const reviewSelectionCommand = vscode.commands.registerCommand('ai-reviewer.reviewSelection', () => __awaiter(this, void 0, void 0, function* () {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage('请先打开一个 Python 文件');
            return;
        }
        const target = getSelectionTarget(editor);
        if (!target) {
            vscode.window.showErrorMessage('请先选中一段代码再执行“选中代码审查”');
            return;
        }
        yield runReview(editor, target);
    }));
    const optimizeSelectionCommand = vscode.commands.registerCommand('ai-reviewer.optimizeSelection', () => __awaiter(this, void 0, void 0, function* () {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage('请先打开一个 Python 文件');
            return;
        }
        const target = getSelectionTarget(editor);
        if (!target) {
            vscode.window.showErrorMessage('请先选中一段代码再执行“选中代码优化”');
            return;
        }
        yield runOptimize(editor, target);
    }));
    context.subscriptions.push(reviewCommand, optimizeCommand, reviewSelectionCommand, optimizeSelectionCommand);
}
exports.activate = activate;
function getWholeDocumentTarget(editor) {
    return {
        code: editor.document.getText(),
        selectionRange: null,
        lineOffset: 0,
        allowedStartLine: 0,
        allowedEndLine: Math.max(0, editor.document.lineCount - 1),
    };
}
function getSelectionTarget(editor) {
    const selection = editor.selection;
    if (selection.isEmpty)
        return null;
    const range = new vscode.Range(selection.start, selection.end);
    return {
        code: editor.document.getText(range),
        selectionRange: range,
        lineOffset: range.start.line,
        allowedStartLine: range.start.line,
        allowedEndLine: range.end.line,
    };
}
function saveReportToWorkspace(content) {
    return __awaiter(this, void 0, void 0, function* () {
        const folders = vscode.workspace.workspaceFolders;
        if (!folders || folders.length === 0) {
            vscode.window.showErrorMessage('请先打开一个工作区文件夹再保存报告');
            return;
        }
        const defaultUri = vscode.Uri.joinPath(folders[0].uri, 'ai-review-report.json');
        const targetUri = yield vscode.window.showSaveDialog({
            defaultUri,
            filters: { JSON: ['json'], Text: ['txt'] },
            saveLabel: '保存审查报告',
        });
        if (!targetUri)
            return;
        const encoder = new TextEncoder();
        yield vscode.workspace.fs.writeFile(targetUri, encoder.encode(content));
        vscode.window.showInformationMessage(`已保存审查报告：${targetUri.fsPath}`);
    });
}
function applyDiagnostics(document, result, collection, lineOffset = 0, allowedStartLine = 0, allowedEndLine = Math.max(0, document.lineCount - 1), selectionRange = null) {
    var _a;
    const issues = (_a = result.issues) !== null && _a !== void 0 ? _a : [];
    const diagnostics = [];
    for (const issue of issues) {
        const lineNumber = parseFirstLineNumber(issue.line_number);
        if (!lineNumber)
            continue;
        const lineIndex = Math.max(0, Math.min(document.lineCount - 1, lineOffset + (lineNumber - 1)));
        if (lineIndex < allowedStartLine || lineIndex > allowedEndLine)
            continue;
        const line = document.lineAt(lineIndex);
        const range = getIssueRange(document, selectionRange, lineIndex);
        const messageParts = [
            toText(issue.risk_description),
            issue.fix_suggestion ? `建议：${toText(issue.fix_suggestion)}` : '',
        ].filter(Boolean);
        const diagnostic = new vscode.Diagnostic(range, messageParts.join('\n'), mapRiskToSeverity(issue.risk_level));
        diagnostic.source = 'AI Reviewer';
        if (issue.violated_rule)
            diagnostic.code = issue.violated_rule;
        diagnostics.push(diagnostic);
    }
    collection.set(document.uri, diagnostics);
}
function getIssueRange(document, selectionRange, lineIndex) {
    const line = document.lineAt(lineIndex);
    if (!selectionRange) {
        return new vscode.Range(line.range.start, line.range.end);
    }
    const start = selectionRange.start;
    const end = selectionRange.end;
    if (start.line === end.line && lineIndex === start.line) {
        return selectionRange;
    }
    if (lineIndex === start.line) {
        return new vscode.Range(start, line.range.end);
    }
    if (lineIndex === end.line) {
        return new vscode.Range(line.range.start, end);
    }
    return new vscode.Range(line.range.start, line.range.end);
}
function revealLine(editor, lineNumber) {
    const lineIndex = Math.max(0, Math.min(editor.document.lineCount - 1, lineNumber - 1));
    const position = new vscode.Position(lineIndex, 0);
    const range = new vscode.Range(position, position);
    editor.selection = new vscode.Selection(position, position);
    editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
}
function replaceWholeDocument(editor, newText) {
    return __awaiter(this, void 0, void 0, function* () {
        const fullRange = new vscode.Range(editor.document.positionAt(0), editor.document.positionAt(editor.document.getText().length));
        yield editor.edit((editBuilder) => editBuilder.replace(fullRange, newText));
    });
}
function replaceRange(editor, range, newText) {
    return __awaiter(this, void 0, void 0, function* () {
        yield editor.edit((editBuilder) => editBuilder.replace(range, newText));
    });
}
function openReportInEditor(content) {
    return __awaiter(this, void 0, void 0, function* () {
        const doc = yield vscode.workspace.openTextDocument({ content, language: 'json' });
        yield vscode.window.showTextDocument(doc, { preview: false });
    });
}
function showReviewResult(result, getReportText, onReveal, onSave, lineOffset = 0) {
    var _a, _b;
    const panel = vscode.window.createWebviewPanel('reviewResult', 'AI 代码审查报告', vscode.ViewColumn.Two, {
        enableScripts: true,
    });
    const getRiskClass = (level) => {
        if (level === '高')
            return 'risk-high';
        if (level === '中')
            return 'risk-medium';
        return 'risk-low';
    };
    const issuesHtml = ((_a = result.issues) !== null && _a !== void 0 ? _a : [])
        .map((i, idx) => {
        var _a;
        const snippetLineNumber = (_a = parseFirstLineNumber(i.line_number)) !== null && _a !== void 0 ? _a : 1;
        const lineNumber = lineOffset + snippetLineNumber;
        const suggestion = i.fix_suggestion ? escapeHtml(i.fix_suggestion) : '（无）';
        const desc = i.risk_description ? escapeHtml(i.risk_description) : '';
        const dim = i.dimension ? escapeHtml(i.dimension) : '';
        const level = i.risk_level ? escapeHtml(i.risk_level) : '低';
        const violated = i.violated_rule ? escapeHtml(i.violated_rule) : '';
        const lineLabel = lineNumber ? escapeHtml(String(lineNumber)) : '';
        return `
                <div class="issue">
                    <div class="header">
                        <span class="level ${getRiskClass(i.risk_level)}">${level}风险</span>
                        <span class="dimension">[${dim}]</span>
                        <span class="line">行号: ${lineLabel}</span>
                    </div>
                    <div class="desc"><b>风险描述:</b> ${desc}</div>
                    <div class="desc"><b>规则:</b> ${violated || '（无）'}</div>
                    <div class="suggestion"><b>修复建议:</b> ${suggestion}</div>
                    <div class="actions">
                        <button data-line="${lineNumber}" data-idx="${idx}">跳转到代码</button>
                    </div>
                </div>
            `;
    })
        .join('');
    panel.webview.html = `
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 20px; color: #cccccc; background: #1e1e1e; }
                .summary { font-size: 1.2em; margin-bottom: 20px; color: #ffffff; border-left: 4px solid #007acc; padding-left: 10px; }
                .toolbar { display: flex; gap: 10px; margin: 12px 0 20px; }
                .issue { border: 1px solid #444444; padding: 15px; margin-bottom: 15px; border-radius: 5px; background: #252526; }
                .header { margin-bottom: 10px; font-weight: bold; border-bottom: 1px solid #444444; padding-bottom: 5px; display: flex; align-items: center; gap: 10px; }
                .risk-high { color: #ff4444; }
                .risk-medium { color: #ffaa00; }
                .risk-low { color: #00aa00; }
                .dimension { color: #55ccff; }
                .desc { margin-bottom: 8px; color: #eeeeee; line-height: 1.5; }
                .suggestion { color: #aaee88; font-family: "Consolas", "Monaco", monospace; background: #1a1a1a; padding: 8px; border-radius: 3px; display: block; margin-top: 5px; white-space: pre-wrap; }
                .actions { margin-top: 10px; }
                button { background: #007acc; color: #ffffff; border: none; padding: 8px 12px; border-radius: 3px; cursor: pointer; }
                button:hover { background: #0062a3; }
            </style>
        </head>
        <body>
            <div class="summary">📋 ${escapeHtml((_b = result.review_summary) !== null && _b !== void 0 ? _b : '')}</div>
            <div class="toolbar">
                <button id="copyReport">复制报告(JSON)</button>
                <button id="saveReport">保存报告(JSON)</button>
                <button id="openReport">在编辑器打开(JSON)</button>
            </div>
            ${issuesHtml}
            <script>
                const vscode = acquireVsCodeApi();
                document.querySelectorAll('button[data-line]').forEach(btn => {
                    btn.addEventListener('click', () => {
                        const line = Number(btn.getAttribute('data-line'));
                        vscode.postMessage({ command: 'reveal', line });
                    });
                });
                document.getElementById('copyReport').addEventListener('click', async () => {
                    vscode.postMessage({ command: 'copyReport' });
                });
                document.getElementById('saveReport').addEventListener('click', async () => {
                    vscode.postMessage({ command: 'saveReport' });
                });
                document.getElementById('openReport').addEventListener('click', async () => {
                    vscode.postMessage({ command: 'openReport' });
                });
            </script>
        </body>
        </html>
    `;
    panel.webview.onDidReceiveMessage((message) => __awaiter(this, void 0, void 0, function* () {
        if (!message || typeof message !== 'object')
            return;
        if (message.command === 'reveal' && typeof message.line === 'number') {
            onReveal(message.line);
            return;
        }
        if (message.command === 'copyReport') {
            const text = getReportText();
            yield vscode.env.clipboard.writeText(text);
            vscode.window.showInformationMessage('已复制审查报告(JSON)到剪贴板');
            return;
        }
        if (message.command === 'saveReport') {
            const text = getReportText();
            yield onSave(text);
            return;
        }
        if (message.command === 'openReport') {
            const text = getReportText();
            yield openReportInEditor(text);
            return;
        }
    }));
}
function showOptimizeResult(oldCode, newCode, onApply) {
    const panel = vscode.window.createWebviewPanel('optimizeResult', 'AI 代码优化对比', vscode.ViewColumn.Two, {
        enableScripts: true,
    });
    panel.webview.html = `
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body { font-family: -apple-system, sans-serif; padding: 20px; color: #ffffff; background: #1e1e1e; }
                pre { background: #2d2d2d; padding: 15px; border-radius: 5px; overflow: auto; border: 1px solid #444; }
                code { font-family: "Consolas", "Monaco", monospace; line-height: 1.5; }
                .title { font-size: 1.2em; margin-bottom: 15px; font-weight: bold; color: #55ccff; }
                .hint { color: #888; font-size: 0.9em; margin-top: 10px; }
                .actions { margin: 12px 0; display: flex; gap: 10px; }
                button { background: #007acc; color: #ffffff; border: none; padding: 10px 14px; border-radius: 3px; cursor: pointer; }
                button:hover { background: #0062a3; }
            </style>
        </head>
        <body>
            <div class="title">✨ AI 优化方案</div>
            <div class="actions">
                <button id="apply">应用到当前文件</button>
                <button id="copy">复制到剪贴板</button>
            </div>
            <pre><code>${escapeHtml(newCode)}</code></pre>
            <div class="hint">提示: 您可以手动复制以上代码并替换原代码中的相应部分。</div>
            <script>
                const vscode = acquireVsCodeApi();
                const optimized = ${JSON.stringify(newCode).replace(/</g, '\\u003c')};
                document.getElementById('apply').addEventListener('click', () => {
                    vscode.postMessage({ command: 'apply', code: optimized });
                });
                document.getElementById('copy').addEventListener('click', async () => {
                    vscode.postMessage({ command: 'copy' });
                });
            </script>
        </body>
        </html>
    `;
    panel.webview.onDidReceiveMessage((message) => __awaiter(this, void 0, void 0, function* () {
        if (message && message.command === 'apply' && typeof message.code === 'string') {
            try {
                yield onApply(newCode);
                vscode.window.showInformationMessage('已应用优化代码到当前文件');
            }
            catch (error) {
                vscode.window.showErrorMessage(`应用失败: ${error.message}`);
            }
        }
        else if (message && message.command === 'copy') {
            yield vscode.env.clipboard.writeText(newCode);
        }
    }));
}
//# sourceMappingURL=extension.js.map