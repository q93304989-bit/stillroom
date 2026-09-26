import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../components"

/* 助手页：一句话跑完整流程，并实时看到每一步。

   页面上只有四块：要什么 → 步骤时间线 → 请示（要人决定时才出现）→ 结果。
   按钮的含义严格对应后端的状态，界面自己不判断任务成败。 */
Item {
    id: page
    objectName: "agentPage"

    property double startedAt: 0
    property int elapsed: 0

    /* 时间线上的行数（界面上看到的步数；测试与截图工具据此确认绑定真的通了） */
    readonly property int stepCount: stepModel.count

    function stepColor(ok, live) {
        if (live && agentBridge.running)
            return theme.accent;
        return ok ? theme.success : theme.dangerText;
    }

    ListModel { id: stepModel }

    /* 另存为：把助手这次出的结果存到用户选的位置（与手动页、历史灯箱同一个动作）。 */
    FileDialog {
        id: agentSavePicker
        title: "另存助手结果"
        fileMode: FileDialog.SaveFile
        defaultSuffix: agentBridge.kind === "video" ? "mp4" : "png"
        nameFilters: agentBridge.kind === "video"
                     ? ["MP4 视频 (*.mp4)", "所有文件 (*)"]
                     : ["PNG 图片 (*.png)", "JPEG 图片 (*.jpg *.jpeg)", "所有文件 (*)"]
        onAccepted: backend.saveAs(selectedFile.toString(),
                                   agentBridge.resultUrl, agentBridge.resultLocalPath)
    }

    /* 计时只为了让人知道「它还在动」，不参与任何判断 */
    Timer {
        interval: 500
        repeat: true
        running: agentBridge.running
        onTriggered: page.elapsed = Math.floor((Date.now() - page.startedAt) / 1000)
    }

    Connections {
        target: agentBridge

        function onStepsCleared() {
            stepModel.clear();
        }
        function onStepAdded(phase, title, detail, ok, seconds) {
            stepModel.append({"stepTitle": title, "stepDetail": detail,
                              "stepOk": ok, "stepSeconds": seconds, "stepLive": true});
        }
        function onStepsReplaced(steps) {
            // 运行结束后用完整步骤（带耗时与结论）替换实时那几条
            stepModel.clear();
            for (var i = 0; i < steps.length; ++i) {
                var s = steps[i];
                stepModel.append({"stepTitle": s.title, "stepDetail": s.detail,
                                  "stepOk": s.ok, "stepSeconds": s.seconds, "stepLive": false});
            }
        }
        function onRunStarted(runId) {
            page.startedAt = Date.now();
            page.elapsed = 0;
        }
    }

    ScrollView {
        id: scroll
        objectName: "agentScroll"          // 截图工具靠它滚到长期档案那一块
        anchors.fill: parent
        contentWidth: availableWidth
        ScrollBar.vertical.policy: ScrollBar.AsNeeded

        Item {
            width: scroll.availableWidth - 40
            implicitHeight: content.implicitHeight + 40

            ColumnLayout {
                id: content
                x: 20
                y: 20
                width: parent.width
                spacing: 16

                AppCard {
                    title: "一句话说清你要什么"

                    AppTextArea {
                        id: request
                        width: parent.width
                        placeholderText: "例：做一张中秋月饼的海报，竖版，国潮插画风格"
                        enabled: !agentBridge.running
                        Keys.onPressed: function (event) {
                            if (event.key === Qt.Key_Return && (event.modifiers & Qt.ControlModifier)) {
                                if (runButton.enabled)
                                    runButton.clicked();
                                event.accepted = true;
                            }
                        }
                    }

                    RowLayout {
                        width: parent.width
                        spacing: 8

                        AppButton {
                            id: runButton
                            primary: true
                            text: agentBridge.running ? "打断" : "开始"
                            enabled: agentBridge.running || request.text.trim() !== ""
                            onClicked: {
                                if (agentBridge.running)
                                    agentBridge.cancel();
                                else
                                    agentBridge.run(request.text);
                            }
                        }
                        AppButton {
                            text: "清空"
                            enabled: !agentBridge.running
                                     && (stepModel.count > 0 || agentBridge.hasResult)
                            onClicked: agentBridge.clear()
                        }
                        AppButton {
                            text: agentBridge.contextMode === "auto" ? "模式：自动" : "模式：草稿"
                            onClicked: agentBridge.setContextMode(
                                agentBridge.contextMode === "auto" ? "draft" : "auto")
                        }
                        Item { Layout.fillWidth: true }
                        Text {
                            visible: agentBridge.running
                            text: "已运行 " + page.elapsed + " 秒"
                            color: theme.textSub
                            font.pixelSize: 12
                        }
                    }

                    Text {
                        width: parent.width
                        text: "助手会依次做：理解需求 → 找参考 → 生成 → 评估 → 交付。"
                              + "评估不满意会自动改提示词重出一版（最多两轮）；拿不准时会停下来问你。"
                              + "（Ctrl+Enter 直接开始）"
                        color: theme.textDisabled
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                }

                AppCard {
                    /* 上下文（草稿模式）：先看后跑——这里是用户唯一能"改得动"的地方。
                       助手找来的参考、你自己补的要求都在这一张卡上，删掉的本次不会再回来。 */
                    id: contextCard
                    title: "上下文（这次拿什么当参考）"
                    visible: agentBridge.hasContext

                    Text {
                        width: parent.width
                        text: agentBridge.contextReason !== ""
                              ? "说明：" + agentBridge.contextReason
                              : "下面是这次要用的参考。不想要的删掉、想加的自己补一句，改完再生成。"
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }

                    RowLayout {
                        width: parent.width
                        spacing: 14
                        Text {
                            text: "来源"
                            color: theme.textSub
                            font.pixelSize: 12
                        }
                        CheckBox {
                            text: "历史参考"
                            font.pixelSize: 12
                            checked: agentBridge.contextSources["history"] === true
                            onToggled: agentBridge.toggleContextSource("history", checked)
                        }
                        CheckBox {
                            text: "知识库"
                            font.pixelSize: 12
                            checked: agentBridge.contextSources["knowledge"] === true
                            onToggled: agentBridge.toggleContextSource("knowledge", checked)
                        }
                        CheckBox {
                            text: "联网线索"
                            font.pixelSize: 12
                            checked: agentBridge.contextSources["web"] === true
                            onToggled: agentBridge.toggleContextSource("web", checked)
                        }
                        CheckBox {
                            id: agentWebImagesBox
                            text: "联网配图"
                            font.pixelSize: 12
                            checked: agentBridge.contextSources["web_images"] === true
                            onToggled: {
                                // 首次打开先过一遍免责声明（与设置页同一份文案）；
                                // 确认框点了确定才真的把这一项打开。
                                if (checked && !settingsBridge.webImagesAcknowledged) {
                                    webImagesDialog.open();
                                    return;
                                }
                                agentBridge.toggleContextSource("web_images", checked);
                            }
                        }
                        AppButton {
                            text: "记住为默认"
                            onClicked: agentBridge.rememberContextSources()
                        }
                        Item { Layout.fillWidth: true }
                    }

                    Text {
                        width: parent.width
                        text: "需求（可以直接改）"
                        color: theme.textSub
                        font.pixelSize: 12
                    }
                    AppField {
                        id: requirementEdit
                        width: parent.width
                        text: agentBridge.contextRequirement
                    }
                    RowLayout {
                        width: parent.width
                        spacing: 8
                        AppButton {
                            text: "保存需求"
                            onClicked: agentBridge.setContextRequirement(requirementEdit.text)
                        }
                        Item { Layout.fillWidth: true }
                    }

                    Column {
                        width: parent.width
                        spacing: 6

                        Repeater {
                            model: agentBridge.contextItems

                            RowLayout {
                                width: parent.width
                                spacing: 8

                                Text {
                                    text: modelData.kindLabel
                                    color: theme.textSub
                                    font.pixelSize: 11
                                    Layout.preferredWidth: 110
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.title
                                    color: modelData.removed ? theme.textDisabled : theme.textMain
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }
                                Text {
                                    text: modelData.removed ? "已删除，本次不会再用" : modelData.origin
                                    color: theme.textDisabled
                                    font.pixelSize: 11
                                    Layout.maximumWidth: 300
                                    elide: Text.ElideRight
                                }
                                AppButton {
                                    visible: !modelData.removed
                                    text: "删掉"
                                    onClicked: agentBridge.removeContextItem(modelData.key)
                                }
                            }
                        }
                    }

                    Text {
                        width: parent.width
                        visible: agentBridge.contextRemoved > 0
                        text: "已删除 " + agentBridge.contextRemoved + " 条（重找一次也不会回来）"
                        color: theme.textDisabled
                        font.pixelSize: 11
                    }

                    RowLayout {
                        width: parent.width
                        spacing: 8
                        AppField {
                            id: notesEdit
                            Layout.fillWidth: true
                            placeholderText: "我补一句：这次的要求 / 禁止项（进提示词）"
                        }
                        AppButton {
                            text: "记下"
                            onClicked: {
                                agentBridge.setContextNotes(notesEdit.text);
                                notesEdit.text = "";
                            }
                        }
                    }
                    Text {
                        width: parent.width
                        visible: agentBridge.contextNotes !== ""
                        text: "已记下：" + agentBridge.contextNotes
                        color: theme.textSub
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }

                    /* 长期档案：把这份上下文存下来，下次一键套用（偏好能沉淀复用）。
                       「设为默认」的那份，之后每份新草稿都会自动带上。 */
                    Text {
                        width: parent.width
                        text: "长期档案（存下来复用；设为默认的新草稿自动带上）"
                        color: theme.textSub
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        width: parent.width
                        spacing: 8
                        AppField {
                            id: profileNameEdit
                            Layout.fillWidth: true
                            placeholderText: "给这份档案起个名字（留空用需求原文）"
                        }
                        AppButton {
                            text: "存为档案"
                            onClicked: {
                                agentBridge.saveProfile(profileNameEdit.text, "", false);
                                profileNameEdit.text = "";
                            }
                        }
                        AppButton {
                            text: "存并设为默认"
                            onClicked: {
                                agentBridge.saveProfile(profileNameEdit.text, "", true);
                                profileNameEdit.text = "";
                            }
                        }
                    }
                    Column {
                        width: parent.width
                        spacing: 6

                        Repeater {
                            model: agentBridge.profiles

                            RowLayout {
                                width: parent.width
                                spacing: 8

                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.name + "（" + modelData.itemCount + " 条）"
                                          + (modelData.isDefault ? " · 默认" : "")
                                    color: modelData.isDefault ? theme.accent : theme.textMain
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }
                                AppButton {
                                    text: "套用"
                                    enabled: agentBridge.hasContext
                                    onClicked: agentBridge.applyProfile(modelData.id)
                                }
                                AppButton {
                                    visible: !modelData.isDefault
                                    text: "设为默认"
                                    onClicked: agentBridge.setDefaultProfile(modelData.id)
                                }
                                AppButton {
                                    text: "删除"
                                    destructive: true
                                    onClicked: agentBridge.deleteProfile(modelData.id)
                                }
                            }
                        }
                    }

                    RowLayout {
                        width: parent.width
                        spacing: 8
                        AppButton {
                            primary: true
                            visible: agentBridge.contextEditable
                            text: "按这份上下文生成"
                            onClicked: agentBridge.confirmContext()
                        }
                        AppButton {
                            visible: agentBridge.contextEditable
                            text: "重找一次"
                            onClicked: agentBridge.redraft()
                        }
                        AppButton {
                            visible: agentBridge.contextEditable
                            text: "丢掉草稿"
                            onClicked: agentBridge.discardContext()
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                AppCard {
                    title: "步骤"
                    visible: stepModel.count > 0

                    Column {
                        width: parent.width
                        spacing: 10

                        Repeater {
                            model: stepModel

                            RowLayout {
                                width: parent.width
                                spacing: 10

                                Rectangle {
                                    width: 9
                                    height: 9
                                    radius: 5
                                    color: page.stepColor(stepOk, stepLive)
                                    Layout.alignment: Qt.AlignTop
                                    Layout.topMargin: 5
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    Text {
                                        text: (index + 1) + ". " + stepTitle
                                        color: theme.textMain
                                        font.pixelSize: 13
                                        font.bold: true
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        visible: stepDetail !== ""
                                        text: stepDetail
                                        color: theme.textSub
                                        font.pixelSize: 12
                                        wrapMode: Text.Wrap
                                    }
                                }

                                Text {
                                    text: stepSeconds > 0 ? stepSeconds.toFixed(1) + "s" : ""
                                    color: theme.textDisabled
                                    font.pixelSize: 12
                                    Layout.alignment: Qt.AlignTop
                                }
                            }
                        }
                    }

                    Text {
                        width: parent.width
                        visible: agentBridge.usageText !== ""
                        text: "本次调用：" + agentBridge.usageText
                        color: theme.textDisabled
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                }

                /* 请示区：只在需要人做决定时出现，四种请示各给一组按钮 */
                AppCard {
                    title: "需要你决定"
                    visible: agentBridge.needsUser

                    Text {
                        width: parent.width
                        text: agentBridge.message
                        color: theme.textMain
                        font.pixelSize: 13
                        wrapMode: Text.Wrap
                    }
                    Text {
                        width: parent.width
                        visible: agentBridge.decisionText !== ""
                        text: agentBridge.decisionText
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }

                    AppField {
                        id: supplement
                        width: parent.width
                        visible: agentBridge.pendingAction === "input"
                        placeholderText: "补充：主体、风格、用途（竖版还是横版）"
                    }

                    RowLayout {
                        width: parent.width
                        spacing: 8

                        AppButton {
                            primary: true
                            visible: agentBridge.pendingAction === "input"
                            text: "补充后重跑"
                            onClicked: agentBridge.continueWith(supplement.text)
                        }
                        AppButton {
                            visible: agentBridge.pendingAction === "input"
                            text: "不补了，直接开跑"
                            onClicked: agentBridge.runAnyway()
                        }
                        AppButton {
                            primary: true
                            visible: agentBridge.pendingAction === "confirm"
                            text: "可用，采纳"
                            onClicked: agentBridge.acceptResult()
                        }
                        AppButton {
                            visible: agentBridge.pendingAction === "confirm"
                            text: "不满意，换一版"
                            onClicked: agentBridge.continueWith("")
                        }
                        AppButton {
                            primary: true
                            visible: agentBridge.pendingAction === "approval"
                            text: "允许并重跑"
                            onClicked: agentBridge.approveAndRetry()
                        }
                        AppButton {
                            primary: true
                            visible: agentBridge.pendingAction === "budget"
                            text: "重新运行"
                            onClicked: agentBridge.rerun()
                        }
                        Item { Layout.fillWidth: true }
                    }

                    Text {
                        width: parent.width
                        text: "「重跑」是新的一次运行（次数与额度重新计算），原结果不会被覆盖或删除。"
                        color: theme.textDisabled
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                }

                AppCard {
                    title: "结果"
                    visible: agentBridge.hasResult || agentBridge.prompt !== ""

                    Image {
                        width: parent.width
                        height: 260
                        visible: agentBridge.kind === "image" && agentBridge.hasResult
                        source: visible ? agentBridge.resultSource : ""
                        fillMode: Image.PreserveAspectFit
                        asynchronous: true
                        cache: false
                    }

                    Text {
                        width: parent.width
                        visible: agentBridge.kind === "video" && agentBridge.hasResult
                        text: agentBridge.resultUrl
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.WrapAnywhere
                    }

                    Text {
                        width: parent.width
                        visible: agentBridge.referenceCount > 0
                        text: "参考了 " + agentBridge.referenceCount + " 张历史图"
                        color: theme.textSub
                        font.pixelSize: 12
                    }

                    Text {
                        width: parent.width
                        visible: agentBridge.prompt !== ""
                        text: "实际用的提示词：" + agentBridge.prompt
                        color: theme.textDisabled
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }

                    Flow {
                        width: parent.width
                        spacing: 8

                        AppButton {
                            text: "打开结果"
                            enabled: agentBridge.hasResult
                            onClicked: agentBridge.openResult()
                        }
                        AppButton {
                            text: "复制地址"
                            enabled: agentBridge.resultUrl !== ""
                            onClicked: backend.copyText(agentBridge.resultUrl)
                        }
                        AppButton {
                            /* 另存为：与手动页、历史灯箱同一个动作（本地缓存优先，没有才下载）。
                               助手页此前漏了这个按钮——手动页有、助手页没有，用户存不下助手出的片子。 */
                            text: "另存为"
                            enabled: agentBridge.hasResult
                            onClicked: {
                                agentSavePicker.currentFile = backend.suggestedFileName(
                                    agentBridge.kind);
                                agentSavePicker.open();
                            }
                        }
                        AppButton {
                            text: "载入到生成页"
                            enabled: agentBridge.prompt !== "" && !agentBridge.running
                            onClicked: agentBridge.loadIntoGenerator()
                        }
                        AppButton {
                            /* 跑完「改参考再跑一次」：把这次实际用的上下文摊成可编辑草稿，
                               改完再确认生成。自动模式下也走这条（自动模式也落了快照）。 */
                            text: "改参考再跑一次"
                            visible: agentBridge.canEditContext
                            enabled: !agentBridge.running
                            onClicked: agentBridge.editContext()
                        }
                        AppButton {
                            text: "再跑一次"
                            enabled: !agentBridge.running && agentBridge.requirement !== ""
                            onClicked: agentBridge.rerun()
                        }
                    }
                }
            }
        }
    }

    /* 联网配图的免责声明：第一次打开这个开关时弹一次（方案 6.2）。
       与设置页是同一份文案（Python 侧 WEB_IMAGES_DISCLAIMER），两处不会说法不一致。 */
    Dialog {
        id: webImagesDialog
        title: "联网配图：先看一眼免责声明"
        modal: true
        anchors.centerIn: parent
        width: 420
        standardButtons: Dialog.Ok | Dialog.Cancel
        Text {
            width: parent.width
            text: settingsBridge.webImagesDisclaimer
            color: theme.textMain
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
        onAccepted: {
            settingsBridge.acknowledgeWebImages();
            agentBridge.toggleContextSource("web_images", true);
        }
        onRejected: agentWebImagesBox.checked = false
    }
}
