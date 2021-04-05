#!/usr/bin/env python3

from pathlib import Path
import cv2
import depthai as dai
import numpy as np
import time
import argparse

labelMap = ["no-person", "person"]

nnPathDefault = str((Path(__file__).parent / Path('models/person-detection_openvino_2021.2_7shave.blob')).resolve().absolute())
videoPathDefault = str((Path(__file__).parent / Path('models/construction_vest.mp4')).resolve().absolute())
parser = argparse.ArgumentParser()
parser.add_argument('-nnPath', help="Path to mobilenet detection network blob", default=nnPathDefault)
parser.add_argument('-v', '--videoPath', help="Path to video frame", default=videoPathDefault)

args = parser.parse_args()

# Start defining a pipeline
pipeline = dai.Pipeline()

# Create neural network input
xiFrame = pipeline.createXLinkIn()
xiFrame.setStreamName("inFrame")
xiFrame.setMaxDataSize(1920*1080*3)

detectionNetwork = pipeline.createMobileNetDetectionNetwork()
objectTracker = pipeline.createObjectTracker()
trackerOut = pipeline.createXLinkOut()

xlinkOut = pipeline.createXLinkOut()

xlinkOut.setStreamName("trackerFrame")
trackerOut.setStreamName("tracklets")

# Create a node to convert the grayscale frame into the nn-acceptable form
manip = pipeline.createImageManip()
manip.initialConfig.setResize(544, 320)
manip.initialConfig.setKeepAspectRatio(False) #squash the image to not lose FOV
# The NN model expects BGR input. By default ImageManip output type would be same as input (gray in this case)
manip.initialConfig.setFrameType(dai.RawImgFrame.Type.BGR888p)
xiFrame.out.link(manip.inputImage)

manipOut = pipeline.createXLinkOut()
manipOut.setStreamName("manip")
manip.out.link(manipOut.input)

nnOut = pipeline.createXLinkOut()
nnOut.setStreamName("nn")
detectionNetwork.out.link(nnOut.input)


# setting node configs
detectionNetwork.setBlobPath(args.nnPath)
detectionNetwork.setConfidenceThreshold(0.5)

manip.out.link(detectionNetwork.input)

objectTracker.passthroughTrackerFrame.link(xlinkOut.input)


objectTracker.setDetectionLabelsToTrack([1])  # track only person
# possible tracking types: ZERO_TERM_COLOR_HISTOGRAM, ZERO_TERM_IMAGELESS
objectTracker.setTrackerType(dai.TrackerType.ZERO_TERM_COLOR_HISTOGRAM)
# take the smallest ID when new object is tracked, possible options: SMALLEST_ID, UNIQUE_ID
objectTracker.setTrackerIdAssigmentPolicy(dai.TrackerIdAssigmentPolicy.SMALLEST_ID)

xiFrame.out.link(objectTracker.inputTrackerFrame)
detectionNetwork.passthrough.link(objectTracker.inputDetectionFrame)

detectionNetwork.out.link(objectTracker.inputDetections)
objectTracker.out.link(trackerOut.input)


# Pipeline defined, now the device is connected to
with dai.Device(pipeline) as device:

    # Start the pipeline
    device.startPipeline()

    qIn = device.getInputQueue(name="inFrame")
    trackerFrameQ = device.getOutputQueue("trackerFrame", 4, False)
    tracklets = device.getOutputQueue("tracklets", 4, False)
    qManip = device.getOutputQueue("manip", maxSize=4, blocking=False)
    qDet = device.getOutputQueue("nn", maxSize=4, blocking=False)

    startTime = time.monotonic()
    counter = 0
    detections = []
    frame = None

    def to_planar(arr: np.ndarray, shape: tuple) -> np.ndarray:
        return cv2.resize(arr, shape).transpose(2, 0, 1).flatten()

    # nn data, being the bounding box locations, are in <0..1> range - they need to be normalized with frame width/height
    def frameNorm(frame, bbox):
        normVals = np.full(len(bbox), frame.shape[0])
        normVals[::2] = frame.shape[1]
        return (np.clip(np.array(bbox), 0, 1) * normVals).astype(int)

    def displayFrame(name, frame):
        for detection in detections:
            bbox = frameNorm(frame, (detection.xmin, detection.ymin, detection.xmax, detection.ymax))
            cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (255, 0, 0), 2)
            cv2.putText(frame, labelMap[detection.label], (bbox[0] + 10, bbox[1] + 20), cv2.FONT_HERSHEY_TRIPLEX, 0.5, 255)
            cv2.putText(frame, f"{int(detection.confidence * 100)}%", (bbox[0] + 10, bbox[1] + 40), cv2.FONT_HERSHEY_TRIPLEX, 0.5, 255)
        cv2.imshow(name, frame)

    cap = cv2.VideoCapture(args.videoPath)
    baseTs = time.monotonic()
    simulatedFps = 30
    while cap.isOpened():
        read_correctly, frame = cap.read()
        if not read_correctly:
            break

        img = dai.ImgFrame()
        img.setType(dai.RawImgFrame.Type.BGR888p)
        img.setData(to_planar(frame, (1280, 720)))
        img.setTimestamp(baseTs)
        baseTs += 1/simulatedFps
        img.setWidth(1280)
        img.setHeight(720)
        qIn.send(img)

        imgFrame = trackerFrameQ.get()
        track = tracklets.get()
        manip = qManip.get()
        inDet = qDet.get()
        detections = inDet.detections
        manipFrame = manip.getCvFrame()
        displayFrame("nn", manipFrame)

        counter+=1
        current_time = time.monotonic()
        if (current_time - startTime) > 1 :
            fps = counter / (current_time - startTime)
            counter = 0
            startTime = current_time

        color = (255, 0, 0)
        trackerFrame = imgFrame.getCvFrame()
        trackletsData = track.tracklets
        for t in trackletsData:
            roi = t.roi.denormalize(trackerFrame.shape[1], trackerFrame.shape[0])
            x1 = int(roi.topLeft().x)
            y1 = int(roi.topLeft().y)
            x2 = int(roi.bottomRight().x)
            y2 = int(roi.bottomRight().y)

            try:
                label = labelMap[t.label]
            except:
                label = t.label

            statusMap = {dai.Tracklet.TrackingStatus.NEW : "NEW", dai.Tracklet.TrackingStatus.TRACKED : "TRACKED", dai.Tracklet.TrackingStatus.LOST : "LOST"}
            cv2.putText(trackerFrame, str(label), (x1 + 10, y1 + 20), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)
            cv2.putText(trackerFrame, f"ID: {[t.id]}", (x1 + 10, y1 + 35), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)
            cv2.putText(trackerFrame, statusMap[t.status], (x1 + 10, y1 + 50), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)
            cv2.rectangle(trackerFrame, (x1, y1), (x2, y2), color, cv2.FONT_HERSHEY_SIMPLEX)


        cv2.imshow("tracker", trackerFrame)

        if cv2.waitKey(1) == ord('q'):
            break
