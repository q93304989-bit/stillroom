import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

/* 结果区：图片页与视频页共用。
   状态一律来自 backend（界面不自己判断任务状态）。 */
AppCard {
    id: panel

    property string kind: "image"      // image / video
    title: "生成结果"

    /* 另存为：把产物存到用户选的位置。
       旧版结果区一直有这个按钮（「保存」→ 系统另存为对话框），迁移到 Qt Quick 时漏了。 */
    FileDialog {
        id: savePicker
        title: "另存结果"
        fileMode: FileDialog.SaveFile
        defaultSuffix: panel.kind === "video" ? "mp4" : "png"
        nameFilters: panel.kind === "video"
                     ? ["MP4 视频 (*.mp4)", "所有文件 (*)"]
                     : ["PNG 图片 (*.png)", "JPEG 图片 (*.jpg *.jpeg)", "所有文件 (*)"]
        onAccepted: backend.saveAs(selectedFile.toString(),
                                   backend.resultUrl, backend.resultLocalPath)
    }

    // 空态
    Text {
        width: parent.width
        visible: !backend.hasResult && !backend.busy && backend.status === "idle"
        text: panel.kind === "image" ? "输入提示词后点「生成」，结果会出现在这里"
                                     : "提交后这里显示进度与结果"
        color: theme.textDisabled
        font.pixelSize: 12
        wrapMode: Text.Wrap
    }

    // 进行中
    Column {
        width: parent.width
        spacing: 6
        visible: backend.busy
        Row {
            spacing: 8
            Text {
                text: backend.statusText
                color: theme.textMain
                font.pixelSize: 13
                font.bold: true
            }
            Text {
                text: backend.toolName
                color: theme.textSub
                font.pixelSize: 12
                visible: backend.toolName !== ""
            }
        }
        Text {
            width: parent.width
            text: backend.message
            color: theme.textSub
            font.pixelSize: 12
            wrapMode: Text.Wrap
        }
    }

    // 失败
    Text {
        width: parent.width
        visible: backend.status === "failed" && backend.errorText !== ""
        text: backend.errorText
        color: theme.dangerText
        font.pixelSize: 12
        wrapMode: Text.Wrap
    }

    // 图片预览
    Image {
        id: preview
        width: parent.width
        height: 260
        visible: panel.kind === "image" && backend.hasResult && backend.status === "succeeded"
        source: visible ? backend.resultSource : ""
        fillMode: Image.PreserveAspectFit
        asynchronous: true
        cache: false
    }

    // 视频信息
    Column {
        width: parent.width
        spacing: 6
        visible: panel.kind === "video" && backend.hasResult && backend.status === "succeeded"
        Text {
            width: parent.width
            text: backend.resultUrl
            color: theme.textSub
            font.pixelSize: 12
            wrapMode: Text.WrapAnywhere
            elide: Text.ElideMiddle
            maximumLineCount: 2
        }
    }

    // 操作
    Flow {
        width: parent.width
        spacing: 8
        visible: backend.hasResult
        AppButton {
            text: "复制地址"
            enabled: backend.resultUrl !== ""
            onClicked: backend.copyText(backend.resultUrl)
        }
        AppButton {
            text: "在浏览器打开"
            enabled: backend.resultUrl !== ""
            onClicked: backend.openUrl(backend.resultUrl)
        }
        AppButton {
            /* 「另存为」：本地缓存优先、没有才下载；这与旧版的保存行为一致 */
            text: "另存为"
            enabled: backend.hasResult
            onClicked: {
                var local = backend.resultLocalPath;
                savePicker.currentFile = backend.suggestedFileName(panel.kind);
                savePicker.open();
            }
        }
        AppButton {
            text: panel.kind === "video" ? "用播放器打开" : "打开本地文件"
            enabled: backend.resultLocalPath !== ""
            onClicked: backend.openPath(backend.resultLocalPath)
        }
    }
}
