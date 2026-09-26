import QtQuick
import QtMultimedia

/* 应用内视频播放（单独一个文件，用 Loader 加载：
   万一目标机器缺 QtMultimedia，也只是这个组件不可用，不会拖垮整个界面）。 */
Item {
    id: player

    property string source: ""
    property bool playing: false

    function togglePlay() {
        if (media.playbackState === MediaPlayer.PlayingState)
            media.pause();
        else
            media.play();
    }

    MediaPlayer {
        id: media
        source: player.source
        autoPlay: true
        onPlaybackStateChanged: player.playing = (playbackState === MediaPlayer.PlayingState)
        onErrorOccurred: player.errorText = errorString
    }

    property string errorText: ""

    VideoOutput {
        anchors.fill: parent
        fillMode: VideoOutput.PreserveAspectFit
    }

    Component.onCompleted: media.videoOutput = children[0]
}
