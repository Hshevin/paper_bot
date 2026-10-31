import SwiftUI

// Explicitly select the longstanding property wrapper. Newer SDKs also expose
// an identically named macro whose plugin is absent from Command Line Tools.
typealias ViewState<Value> = SwiftUI.State<Value>
