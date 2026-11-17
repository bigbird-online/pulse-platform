import AppKit
import SwiftUI

struct CostRecordDetail: View {
    let row: CostQueryGroup
    let view: CostQueryView
    let onClose: () -> Void

    /// What the row is about, in words a person reads first.
    private var headline: String {
        if let item = row.workItem { return item.title }
        if let job = row.scheduledJobText { return job }
        switch row.key["wi"] ?? "unassigned" {
        case "cron": return "定时任务（不属于 WI）"
        case "none": return "临时任务（不属于 WI）"
        case "ambiguous": return "WI 待核验"
        case "unassigned": return "未关联 WI"
        default: return row.key["wi"] ?? "未关联 WI"
        }
    }
    private var isWorkItem: Bool {
        !row.isNonWI && !["unassigned", "ambiguous"].contains(row.key["wi"] ?? "unassigned")
    }
    private var facts: String {
        [CostTableColumn.amount.text(row) == "未提供" ? nil : CostTableColumn.amount.text(row),
         "\(CostDetailContent.dateText(row.detail?.firstUsageAt)) 至 \(CostDetailContent.dateText(row.detail?.lastUsageAt))",
         CostTableColumn.agent.text(row), CostTableColumn.entrypoint.text(row), row.key["model"]]
            .compactMap { $0 }.joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(headline).font(.caption.weight(.semibold))
                Spacer()
                Button("收起") { onClose() }.buttonStyle(.plain).font(.caption2)
            }
            if let item = row.workItem {
                if let summary = item.summary { Text(summary) }
                Text("\(item.id) · \(item.statusText)").foregroundStyle(.secondary)
                if let parent = item.parent {
                    Text("子 WI · 父 WI：" + ([parent, row.parentWorkItem?.title].compactMap { $0 }.joined(separator: " "))
                         + "；本行只计本子 WI").foregroundStyle(.secondary)
                }
                if let family = row.family {
                    Text(family.text + "；本行仍只计本 WI，总计不重复相加")
                }
            } else if isWorkItem, let wi = row.key["wi"] {
                Text(wi).foregroundStyle(.secondary)
            }
            Text(facts)
            if !row.cost.note.isEmpty { Text("需处理：" + row.cost.note).foregroundStyle(.orange) }
            if row.hasHolderSupplement {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text("●").foregroundStyle(Color(nsColor: CostMarks.holderSupplement))
                    Text("其中 \(row.holderSupplementCount) 条用量所属请求未申报 WI，按该会话同期写入的 WI 归入。")
                }
            }
            if isWorkItem {
                CostDetailContent.pullRequestList(row.pullRequests, result: view.validPullRequests)
                Text("验收成果").fontWeight(.medium)
                let refs = Set(row.outcomes?.refs ?? [])
                if row.outcomeCount == 0 {
                    Text("本期没有成果记录").foregroundStyle(.secondary)
                } else {
                    CostDetailContent.outcomeList(view.validOutcomes?.items.filter { refs.contains($0.id) } ?? [], result: refs.isEmpty ? nil : view.validOutcomes)
                }
            }
            DisclosureGroup("核对凭据") { evidence }
        }.font(.caption2).padding(9)
         .frame(maxWidth: .infinity, alignment: .leading)
         .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
    }

    @ViewBuilder private var evidence: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let attribution = row.attribution, !attribution.summary.isEmpty { Text("归属方式：" + attribution.summary) }
            if !row.cost.proofGapNote.isEmpty { Text("完整性未证明：" + row.cost.proofGapNote) }
            Text("租户：\(row.tenantText)\(row.tenantSourceText.map { "（来源：\($0)）" } ?? "")")
            Text("节点：\(CostTableColumn.node.text(row)) · Runtime：\(CostTableColumn.runtime.text(row))")
            CostDetailContent.tokenDetails(row.usage?.tokens)
            Text("精确标准金额：\(row.cost.exactAmount.map { "USD " + $0 } ?? "未提供")")
            Text("价表：\(CostDetailContent.dateText(view.catalogObservedAt))")
            if isWorkItem { Text("合并 PR 与验收成果都按 WI 引用，跨行不重复计数") }
            if let calls = row.detail?.subcalls {
                if calls.isValid(eventCount: row.usage?.eventCount ?? 0, runtime: row.key["runtime"]) {
                    Text("调用来源 · \(calls.eventCount) 条子调用用量 · 根入口：\(CostQueryGroup(key: ["entrypoint": row.key["entrypoint"] ?? "unassigned"], cost: row.cost).title)")
                    ForEach(calls.items) { call in
                        Text(call.chain).fontWeight(.medium)
                        if let label = call.label { Text("来源任务：" + label) }
                        Text("\(call.eventCount) 条用量 · " + (call.basis == "reviewed_launch" ? "经原始凭据复核" : "原生工具执行回执"))
                        CostDetailContent.evidenceReference("父会话", call.parentScopeRef)
                        CostDetailContent.evidenceReference("工具调用", call.callRef)
                        CostDetailContent.evidenceReference("子会话", call.childScopeRef)
                    }
                    if calls.itemCount > calls.items.count {
                        Text("另有 \(calls.itemCount - calls.items.count) 条调用关系，完整列表保存在同水位原查询中。")
                    }
                } else { Text("子调用凭据待核验，未采用来源标签") }
            }
            Text("期间 \(view.query.fromDate) 至 \(view.query.toDate)（不含结束日）· 水位 \(view.watermark)")
            if let hash = view.catalogSha256 { CostDetailContent.evidenceReference("价表摘要", hash) }
            CostDetailContent.evidenceReference("报告", view.reportSha256)
            if let tenant = row.tenant {
                ForEach(tenant.bindingRefs.prefix(5), id: \.self) { CostDetailContent.evidenceReference("租户登记", $0) }
            }
            if let detail = row.detail {
                Text("用量引用 \(detail.factRefs.count)/\(detail.factRefCount) · 归属引用 \(detail.attributionRefs.count)/\(detail.attributionRefCount)")
                ForEach(detail.factRefs + detail.attributionRefs, id: \.self) { Text($0).textSelection(.enabled) }
            }
        }.foregroundStyle(.secondary)
    }
}

/// Same original-query detail content in both reading surfaces.
enum CostDetailContent {
    /// A WI's output: the PRs the core reviewed and merged, with scores and cost per PR.
    static func pullRequestList(_ prs: CostRowPullRequests?, result: WIPullRequestOutputs?) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if let count = prs?.count {
                Text("产出：合并 PR \(count) 个").fontWeight(.medium)
                if let item = prs?.item {
                    Text(item.costText)
                    ForEach(item.pullRequests) { pr in
                        HStack(alignment: .firstTextBaseline) {
                            Text(pr.title)
                            Spacer()
                            Text(pr.reviewText).foregroundStyle(.secondary)
                        }
                        Text("合并于 " + dateText(pr.mergedAt) + (pr.mergeCommit.map { " · " + String($0.prefix(10)) } ?? ""))
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Text("本期没有经内核合并的 PR").foregroundStyle(.secondary)
                }
            } else {
                Text("产出：合并 PR 未提供").fontWeight(.medium)
                Text(result?.note ?? "合并 PR 记录未提供，不等于 0 个").foregroundStyle(.secondary)
            }
        }.font(.caption2).frame(maxWidth: .infinity, alignment: .leading)
    }

    /// GitHub merges without a kernel record and direct pushes to main.
    static func anomalyList(_ anomalies: PullRequestAnomalies) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("读取于 " + dateText(anomalies.observedAt)).foregroundStyle(.secondary)
            if anomalies.status != "verified" {
                Text(PullRequestAnomalies.reasonText(anomalies.reason) + "；未核验部分不计为 0。").foregroundStyle(.secondary)
            }
            if let enforced = anomalies.enforcedSince {
                Text("\(enforced) 起 GitHub 合并须经内核；此前的只作参考").foregroundStyle(.secondary)
            } else {
                Text("关卡尚未强制：GitHub 直接合并只作参考，不计入异常").foregroundStyle(.secondary)
            }
            ForEach(anomalies.unrecordedMerges?.items ?? []) { Text($0.line("未经内核合并")) }
            ForEach(anomalies.unenforcedMerges?.items ?? []) {
                Text($0.line("参考 · GitHub 直接合并")).foregroundStyle(.secondary)
            }
            ForEach(anomalies.directCommits?.items ?? []) { Text($0.line) }
            if anomalies.unlistedCount > 0 {
                Text("另有 \(anomalies.unlistedCount) 项未列出").foregroundStyle(.secondary)
            }
        }.font(.caption2).frame(maxWidth: .infinity, alignment: .leading)
    }

    static func outcomeList(_ items: [WIOutcomeItem], result: WIOutcomeResult?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(result?.note ?? "Outcome 未提供")
            ForEach(items) { item in
                VStack(alignment: .leading, spacing: 4) {
                    HStack { Text(item.wi).fontWeight(.medium); Spacer(); Text(item.statusText) }
                    Text(item.title ?? "成果名称未随上游凭据提供")
                    if let summary = item.summary { Text(summary) }
                    Text(dateText(item.occurredAt)).foregroundStyle(.secondary)
                    DisclosureGroup("成果、版本与验收凭据") {
                        evidenceReference("Outcome", item.outcomeRef)
                        evidenceReference("版本", item.candidateRef)
                        evidenceReference("产物", item.artifactRef)
                        evidenceReference("验收依据", item.acceptanceRef)
                        evidenceReference("记录", item.factRef)
                        Text("审核状态：\(item.reviewDecisions.joined(separator: "、"))")
                        ForEach(item.reviewRefs + item.evidenceRefs, id: \.self) { Text($0).textSelection(.enabled) }
                    }
                }.padding(8).background(Color.secondary.opacity(0.06), in: RoundedRectangle(cornerRadius: 5))
            }
        }.font(.caption2).frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Anomalies need a correction; proof gaps only qualify completeness.
    @ViewBuilder static func reasonLines(_ cost: QueryMachineCost) -> some View {
        if !cost.note.isEmpty { Text("需处理：" + cost.note).foregroundStyle(.orange) }
        if !cost.proofGapNote.isEmpty { Text("完整性未证明：" + cost.proofGapNote).foregroundStyle(.secondary) }
    }

    static func tokenDetails(_ tokens: [String: CostTokenQuantity]?) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(["uncached_input", "cache_write", "output", "cache_read"], id: \.self) { key in
                let label = ["uncached_input":"新输入", "cache_write":"缓存创建", "output":"输出 Token", "cache_read":"缓存复读"][key]!
                let unknown = tokens?[key]?.unknownEventCount ?? 0
                StatsDetailRow(label, StatsFormat.grouped(tokens?[key]?.known) + (unknown > 0 ? " · 另有 \(unknown) 条未知" : ""))
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    static func evidenceReference(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).foregroundStyle(.secondary)
            Text(value).font(.system(size: 10, design: .monospaced)).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }.frame(maxWidth: .infinity, alignment: .leading).multilineTextAlignment(.leading)
    }

    static func dateText(_ date: Date?) -> String {
        guard let date else { return "未知" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.dateFormat = "MM-dd HH:mm（北京）"
        return formatter.string(from: date)
    }
}
