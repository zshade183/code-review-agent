import * as vscode from 'vscode';
import * as http from 'http';
import * as https from 'https';

type ReviewIssue = {
    dimension?: string;
    line_number?: string;
    violated_rule?: string;
    risk_level?: string;
    risk_description?: string;
    fix_suggestion?: string;
};

type ReviewResult = {
    review_summary?: string;
    issues?: ReviewIssue[];
};

type CodeTarget = {
    code: string;
    selectionRange: vscode.Range | null;
    lineOffset: number;
    allowedStartLine: number;
    allowedEndLine: number;
};

function postJson<T>(urlString: string, body: unknown): Promise<T> {
    const url = new URL(urlString);
    const data = JSON.stringify(body ?? {});
    const isHttps = url.protocol === 'https:';
    const requestImpl = isHttps ? https.request : http.request;

    return new Promise((resolve, reject) => {
        const req = requestImpl(
            {
                protocol: url.protocol,
                hostname: url.hostname,
                port: url.port ? Number(url.port) : (isHttps ? 443 : 80),
                path: `${url.pathname}${url.search}`,
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json; charset=utf-8',
                    'Content-Length': Buffer.byteLength(data),
                },
            },
            (res) => {
                const statusCode = res.statusCode ?? 0;
                const chunks: Buffer[] = [];
                res.on('data', (chunk) => chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
                res.on('end', () => {
                    const raw = Buffer.concat(chunks).toString('utf-8');
                    if (statusCode < 200 || statusCode >= 300) {
                        reject(new Error(`HTTP ${statusCode}: ${raw || res.statusMessage || 'Request failed'}`));
                        return;
                    }
                    try {
                        resolve(JSON.parse(raw) as T);
                    } catch (e) {
                        reject(new Error(`响应不是合法 JSON：${raw.slice(0, 300)}`));
                    }
                });
            }
        );

        req.on('error', (err) => reject(err));
        req.setTimeout(360000, () => req.destroy(new Error('请求超时，请检查后台服务')));
        req.write(data);
        req.end();
    });
}

function parseFirstLineNumber(value: string | undefined): number | undefined {
    if (!value) return undefined;
    const match = value.match(/\d+/);
    if (!match) return undefined;
    const n = Number(match[0]);
    if (!Number.isFinite(n)) return undefined;
    return n;
}

function mapRiskToSeverity(level: string | undefined): vscode.DiagnosticSeverity {
    if (level === '高') return vscode.DiagnosticSeverity.Error;
    if (level === '中') return vscode.DiagnosticSeverity.Warning;
    return vscode.DiagnosticSeverity.Information;
}

function safeStringify(value: unknown): string | undefined {
    const seen = new WeakSet<object>();
    try {
        return JSON.stringify(
            value,
            (_key, v) => {
                if (v && typeof v === 'object') {
                    if (seen.has(v)) return '[Circular]';
                    seen.add(v);
                }
                return v;
            },
            2
        );
    } catch {
        return undefined;
    }
}

function toText(value: unknown): string {
    if (typeof value === 'string') return value;
    if (value === null || value === undefined) return '';
    const json = safeStringify(value);
    if (typeof json === 'string') return json;
    try {
        return String(value);
    } catch {
        return '';
    }
}

function escapeHtml(value: unknown): string {
    const unsafe = toText(value);
    return unsafe
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function toJsonPretty(value: unknown): string {
    const json = safeStringify(value);
    return json ?? toText(value);
}

function toJsonReport(value: unknown): string {
    const json = safeStringify(value);
    if (typeof json === 'string') return json;
    return JSON.stringify({ text: toText(value) }, null, 2);
}

function normalizeReviewResult(input: unknown): ReviewResult {
    let data: any = input;

    if (typeof data === 'string') {
        try {
            data = JSON.parse(data);
        } catch {
            return { review_summary: data, issues: [] };
        }
    }

    if (data && typeof data === 'object' && 'result' in data) {
        data = (data as any).result;
    }

    const issues = Array.isArray(data?.issues) ? data.issues : [];
    return {
        review_summary: typeof data?.review_summary === 'string' ? data.review_summary : toText(data?.review_summary),
        issues: issues.map((i: any) => ({
            dimension: typeof i?.dimension === 'string' ? i.dimension : toText(i?.dimension),
            line_number: typeof i?.line_number === 'string' ? i.line_number : toText(i?.line_number),
            violated_rule: typeof i?.violated_rule === 'string' ? i.violated_rule : toText(i?.violated_rule),
            risk_level: typeof i?.risk_level === 'string' ? i.risk_level : toText(i?.risk_level),
            risk_description: typeof i?.risk_description === 'string' ? i.risk_description : toText(i?.risk_description),
            fix_suggestion: typeof i?.fix_suggestion === 'string' ? i.fix_suggestion : toText(i?.fix_suggestion),
        })),
    };
}

export function activate(context: vscode.ExtensionContext) {
    const diagnosticCollection = vscode.languages.createDiagnosticCollection('ai-reviewer');
    context.subscriptions.push(diagnosticCollection);

    const apiHost = () => (vscode.workspace.getConfiguration('aiReviewer').get<string>('apiHost') || 'http://127.0.0.1:8000').replace(/\/+$/, '');

    // 编辑器场景默认 auto：既不是单 Agent，也不总是跑完整四领域。
    const strategy = () => {
        const value = (vscode.workspace.getConfiguration('aiReviewer').get<string>('strategy') || 'auto').trim().toLowerCase();
        return value === 'fast' || value === 'full' ? value : 'auto';
    };

    const runReview = async (editor: vscode.TextEditor, target: CodeTarget) => {
        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'AI 正在多 Agent 审查代码...',
                cancellable: false,
            },
            async () => {
                try {
                    const data = await postJson<{ result: unknown }>(
                        `${apiHost()}/review?strategy=${encodeURIComponent(strategy())}`,
                        { code: target.code }
                    );
                    const result = normalizeReviewResult(data.result);
                    applyDiagnostics(
                        editor.document,
                        result,
                        diagnosticCollection,
                        target.lineOffset,
                        target.allowedStartLine,
                        target.allowedEndLine,
                        target.selectionRange
                    );
                    showReviewResult(
                        result,
                        () => toJsonReport(result),
                        (line) => revealLine(editor, line),
                        async (content) => saveReportToWorkspace(content),
                        target.lineOffset
                    );
                } catch (error: any) {
                    vscode.window.showErrorMessage(`审查失败: ${error.message}`);
                }
            }
        );
    };

    const runOptimize = async (editor: vscode.TextEditor, target: CodeTarget) => {
        const sourceVersion = editor.document.version;
        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'AI 正在尝试优化代码...',
                cancellable: false,
            },
            async () => {
                try {
                    const data = await postJson<{ result: string }>(
                        `${apiHost()}/optimize?strategy=${encodeURIComponent(strategy())}`,
                        { code: target.code }
                    );
                    showOptimizeResult(target.code, data.result ?? '', async (optimized) => {
                        if (editor.document.isClosed || editor.document.version !== sourceVersion) {
                            throw new Error('文件已修改或关闭，请重新生成优化方案');
                        }
                        if (target.selectionRange) {
                            await replaceRange(editor, target.selectionRange, optimized);
                        } else {
                            await replaceWholeDocument(editor, optimized);
                        }
                    });
                } catch (error: any) {
                    vscode.window.showErrorMessage(`优化失败: ${error.message}`);
                }
            }
        );
    };

    const reviewCommand = vscode.commands.registerCommand('ai-reviewer.review', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage('请先打开一个 Python 文件');
            return;
        }
        const target = getWholeDocumentTarget(editor);
        await runReview(editor, target);
    });

    const optimizeCommand = vscode.commands.registerCommand('ai-reviewer.optimize', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage('请先打开一个 Python 文件');
            return;
        }
        const target = getWholeDocumentTarget(editor);
        await runOptimize(editor, target);
    });

    const reviewSelectionCommand = vscode.commands.registerCommand('ai-reviewer.reviewSelection', async () => {
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
        await runReview(editor, target);
    });

    const optimizeSelectionCommand = vscode.commands.registerCommand('ai-reviewer.optimizeSelection', async () => {
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
        await runOptimize(editor, target);
    });

    context.subscriptions.push(reviewCommand, optimizeCommand, reviewSelectionCommand, optimizeSelectionCommand);
}

function getWholeDocumentTarget(editor: vscode.TextEditor): CodeTarget {
    return {
        code: editor.document.getText(),
        selectionRange: null,
        lineOffset: 0,
        allowedStartLine: 0,
        allowedEndLine: Math.max(0, editor.document.lineCount - 1),
    };
}

function getSelectionTarget(editor: vscode.TextEditor): CodeTarget | null {
    const selection = editor.selection;
    if (selection.isEmpty) return null;
    const range = new vscode.Range(selection.start, selection.end);
    return {
        code: editor.document.getText(range),
        selectionRange: range,
        lineOffset: range.start.line,
        allowedStartLine: range.start.line,
        allowedEndLine: range.end.line,
    };
}

async function saveReportToWorkspace(content: string) {
    const folders = vscode.workspace.workspaceFolders;
    if (!folders || folders.length === 0) {
        vscode.window.showErrorMessage('请先打开一个工作区文件夹再保存报告');
        return;
    }

    const defaultUri = vscode.Uri.joinPath(folders[0].uri, 'ai-review-report.json');
    const targetUri = await vscode.window.showSaveDialog({
        defaultUri,
        filters: { JSON: ['json'], Text: ['txt'] },
        saveLabel: '保存审查报告',
    });
    if (!targetUri) return;

    const encoder = new TextEncoder();
    await vscode.workspace.fs.writeFile(targetUri, encoder.encode(content));
    vscode.window.showInformationMessage(`已保存审查报告：${targetUri.fsPath}`);
}

function applyDiagnostics(
    document: vscode.TextDocument,
    result: ReviewResult,
    collection: vscode.DiagnosticCollection,
    lineOffset: number = 0,
    allowedStartLine: number = 0,
    allowedEndLine: number = Math.max(0, document.lineCount - 1),
    selectionRange: vscode.Range | null = null
) {
    const issues = result.issues ?? [];
    const diagnostics: vscode.Diagnostic[] = [];

    for (const issue of issues) {
        const lineNumber = parseFirstLineNumber(issue.line_number);
        if (!lineNumber) continue;
        const lineIndex = Math.max(0, Math.min(document.lineCount - 1, lineOffset + (lineNumber - 1)));
        if (lineIndex < allowedStartLine || lineIndex > allowedEndLine) continue;
        const line = document.lineAt(lineIndex);
        const range = getIssueRange(document, selectionRange, lineIndex);
        const messageParts = [
            toText(issue.risk_description),
            issue.fix_suggestion ? `建议：${toText(issue.fix_suggestion)}` : '',
        ].filter(Boolean);
        const diagnostic = new vscode.Diagnostic(range, messageParts.join('\n'), mapRiskToSeverity(issue.risk_level));
        diagnostic.source = 'AI Reviewer';
        if (issue.violated_rule) diagnostic.code = issue.violated_rule;
        diagnostics.push(diagnostic);
    }

    collection.set(document.uri, diagnostics);
}

function getIssueRange(
    document: vscode.TextDocument,
    selectionRange: vscode.Range | null,
    lineIndex: number
): vscode.Range {
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

function revealLine(editor: vscode.TextEditor, lineNumber: number) {
    const lineIndex = Math.max(0, Math.min(editor.document.lineCount - 1, lineNumber - 1));
    const position = new vscode.Position(lineIndex, 0);
    const range = new vscode.Range(position, position);
    editor.selection = new vscode.Selection(position, position);
    editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
}

async function replaceWholeDocument(editor: vscode.TextEditor, newText: string) {
    const fullRange = new vscode.Range(
        editor.document.positionAt(0),
        editor.document.positionAt(editor.document.getText().length)
    );
    await editor.edit((editBuilder) => editBuilder.replace(fullRange, newText));
}

async function replaceRange(editor: vscode.TextEditor, range: vscode.Range, newText: string) {
    await editor.edit((editBuilder) => editBuilder.replace(range, newText));
}

async function openReportInEditor(content: string) {
    const doc = await vscode.workspace.openTextDocument({ content, language: 'json' });
    await vscode.window.showTextDocument(doc, { preview: false });
}

function showReviewResult(
    result: ReviewResult,
    getReportText: () => string,
    onReveal: (line: number) => void,
    onSave: (content: string) => Promise<void>,
    lineOffset: number = 0
) {
    const panel = vscode.window.createWebviewPanel('reviewResult', 'AI 代码审查报告', vscode.ViewColumn.Two, {
        enableScripts: true,
    });

    const getRiskClass = (level: string | undefined) => {
        if (level === '高') return 'risk-high';
        if (level === '中') return 'risk-medium';
        return 'risk-low';
    };

    const issuesHtml = (result.issues ?? [])
        .map((i, idx) => {
            const snippetLineNumber = parseFirstLineNumber(i.line_number) ?? 1;
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
            <div class="summary">📋 ${escapeHtml(result.review_summary ?? '')}</div>
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

    panel.webview.onDidReceiveMessage(async (message) => {
        if (!message || typeof message !== 'object') return;
        if (message.command === 'reveal' && typeof message.line === 'number') {
            onReveal(message.line);
            return;
        }
        if (message.command === 'copyReport') {
            const text = getReportText();
            await vscode.env.clipboard.writeText(text);
            vscode.window.showInformationMessage('已复制审查报告(JSON)到剪贴板');
            return;
        }
        if (message.command === 'saveReport') {
            const text = getReportText();
            await onSave(text);
            return;
        }
        if (message.command === 'openReport') {
            const text = getReportText();
            await openReportInEditor(text);
            return;
        }
    });
}

function showOptimizeResult(oldCode: string, newCode: string, onApply: (code: string) => Promise<void>) {
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

    panel.webview.onDidReceiveMessage(async (message) => {
        if (message && message.command === 'apply' && typeof message.code === 'string') {
            try {
                await onApply(newCode);
                vscode.window.showInformationMessage('已应用优化代码到当前文件');
            } catch (error: any) {
                vscode.window.showErrorMessage(`应用失败: ${error.message}`);
            }
        } else if (message && message.command === 'copy') {
            await vscode.env.clipboard.writeText(newCode);
        }
    });
}
